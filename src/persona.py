"""一份问卷 = 一个人：人口学字段全部从同一个 persona 派生。

WHY：v3.1 之前填空是三个互不相干的随机池 —— 姓名从混合池抽（男名女名都往里塞，
却没有人绑）、手机是扁平号段表、地址只有硬编码的 20 个城市，于是同一份问卷里可以
同时出现「张伟（女名池）· 138xxxx · 深圳市南山区 · 3 岁 · 乌鲁木齐某公司」。
每一格单看都合法，连起来是个不存在的人 —— 平台不会拦，但这是数据造假里最容易被
专业评委一眼看穿的一种，而本工具的立身点恰恰是「像真人」。

现在一个人一次定：性别 → 年龄段 → 学历 → 职业 → 收入 → 婚姻 → 子女 → 省市，
姓名 / 身份证 / 手机 / 邮箱 / 地址 / 年龄 / 公司全部从这份画像派生，同一次提交内
自洽，跨提交之间会换人。

对标 SurveyController 的 ``core/persona``（条件概率链 + 每份问卷一个 thread-local
画像 + 身份证真校验位），**补上它没有的那一环**：它的代理出口省份与身份证前 6 位
各自随机、互不知情（见 memory: project-benchmark-surveycontroller），我们让地区
成为画像的单点真源。数据与校验位算法自行整理，不取它的码与数据文件（GPL-3.0）。

已知边界（刻意不做，避免看起来更假）：
- 市辖区级：地址到市为止，不编区县名 —— 编错了比不编更像真的。
- 身份证前 6 位按「省码 + 省内市级序号」组装，绝大多数与 GB/T 2260 一致，个别省
  有盟 / 地区段造成的断号；校验位是真算的（ISO 7064 MOD 11-2），第 17 位奇偶与
  性别一致。
- 手机号只保证号段格式合法；号段→归属地映射需要一张随版本腐烂的大表，收益不配。
"""

from __future__ import annotations

import calendar
import random
import threading
from dataclasses import dataclass
from datetime import date

# ============================================================================
#  姓名池（性别分池）与常见姓氏罗马化
# ============================================================================
_SURNAMES = (
    "王李张刘陈杨赵黄周吴徐孙胡朱高林何郭马罗梁宋郑谢韩唐冯于董萧程曹袁邓许傅沈曾彭吕"
    "苏卢蒋蔡贾丁魏薛叶阎余潘杜戴夏钟汪田任姜范方石姚谭廖邹熊金陆郝孔白崔康毛邱秦"
    "江史顾侯邵孟万段雷钱汤尹黎易常武乔贺赖龚文"
)
#: 男名常用字 / 词
_GIVEN_NAMES_M = (
    "伟 强 磊 军 洋 勇 杰 涛 明 超 平 刚 建国 建华 志强 晓明 志伟 浩宇 浩然 宇航 "
    "子轩 文博 思远 俊杰 睿 皓 博文 子豪 天佑 润泽 承恩 国栋 永康 德辉".split()
)
#: 女名常用字 / 词
_GIVEN_NAMES_F = (
    "芳 娜 敏 静 丽 娟 艳 霞 秀兰 桂英 玉兰 秀英 建华 思琪 梓萱 雨桐 梦瑶 佳怡 "
    "雨萱 欣怡 梓涵 诗涵 语桐 若曦 紫涵 静怡 晨曦 悦 晓燕 淑华 慧 雅琴".split()
)
#: 姓名里男女都用到的字（抽单字名时按这个比例混入，避免"一看就是同性别池"）
_GIVEN_NAMES_UNISEX = " 平 明 杰 涛 静 丽 强 磊".split()

_SURNAME_PINYIN = {
    "王": "wang", "李": "li", "张": "zhang", "刘": "liu", "陈": "chen",
    "杨": "yang", "赵": "zhao", "黄": "huang", "周": "zhou", "吴": "wu",
    "徐": "xu", "孙": "sun", "胡": "hu", "朱": "zhu", "高": "gao",
    "林": "lin", "何": "he", "郭": "guo", "马": "ma", "罗": "luo",
    "梁": "liang", "宋": "song", "郑": "zheng", "谢": "xie", "韩": "han",
    "唐": "tang", "冯": "feng", "于": "yu", "董": "dong", "萧": "xiao",
    "程": "cheng", "曹": "cao", "袁": "yuan", "邓": "deng", "许": "xu",
    "傅": "fu", "沈": "shen", "曾": "zeng", "彭": "peng", "吕": "lv",
    "苏": "su", "卢": "lu", "蒋": "jiang", "蔡": "cai", "贾": "jia",
    "丁": "ding", "魏": "wei", "薛": "xue", "叶": "ye", "余": "yu",
}

_EMAIL_DOMAINS = (
    "qq.com", "163.com", "126.com", "gmail.com", "outlook.com",
    "sina.com", "foxmail.com", "hotmail.com", "icloud.com",
)
_MOBILE_PREFIXES = (
    130, 131, 132, 133, 134, 135, 136, 137, 138, 139,
    150, 151, 152, 153, 155, 156, 157, 158, 159,
    176, 177, 178, 180, 181, 182, 183, 184, 185, 186, 187, 188, 189,
    198, 199,
)

# ============================================================================
#  地区：省级码（GB/T 2260 的两位段）+ 省内地级市名录（带真实 4 位市码）
# ============================================================================
#: (省码, 省名, 是否直辖市, [(地级市, 4 位市码)])。直辖市把省名本身当唯一"市"。
#: v4.2 起市码**真码直存**：此前 6 位区划 = 省码 + (名录下标+1)，而名录是
#: 截断的子集（河北 8/11、山东 11/17…），只要某个省的子集不在 GB 序的标准位置上，
#: 算出来的前 6 位就会张冠李戴（山东临沂曾拿到日照的 3711 段）——批量数据可被
#: 区划码反查识破，与"像真人"的立身点冲突。名录刻意保持子集（抽样分布只取主要城市），
#: 但每一位都带上它在 GB/T 2260 里的真实市码。
_REGIONS: tuple[tuple[int, str, bool, tuple[tuple[str, str], ...]], ...] = (
    (11, "北京市", True, (("北京市", "01"),)),
    (12, "天津市", True, (("天津市", "01"),)),
    (13, "河北省", False, (("石家庄市", "01"), ("唐山市", "02"), ("秦皇岛市", "03"),
                           ("邯郸市", "04"), ("邢台市", "05"), ("保定市", "06"),
                           ("沧州市", "09"), ("廊坊市", "10"))),
    (14, "山西省", False, (("太原市", "01"), ("大同市", "02"), ("阳泉市", "03"),
                           ("长治市", "04"), ("晋城市", "05"), ("运城市", "10"))),
    (15, "内蒙古自治区", False, (("呼和浩特市", "01"), ("包头市", "02"), ("乌海市", "03"),
                                 ("赤峰市", "04"), ("通辽市", "05"), ("鄂尔多斯市", "06"))),
    (21, "辽宁省", False, (("沈阳市", "01"), ("大连市", "02"), ("鞍山市", "03"),
                           ("抚顺市", "04"), ("锦州市", "07"), ("营口市", "08"),
                           ("阜新市", "09"), ("辽阳市", "10"), ("盘锦市", "11"))),
    (22, "吉林省", False, (("长春市", "01"), ("吉林市", "02"), ("四平市", "03"),
                           ("辽源市", "04"), ("通化市", "05"), ("白山市", "06"),
                           ("松原市", "07"))),
    (23, "黑龙江省", False, (("哈尔滨市", "01"), ("齐齐哈尔市", "02"), ("鸡西市", "03"),
                             ("鹤岗市", "04"), ("双鸭山市", "05"), ("大庆市", "06"),
                             ("伊春市", "07"), ("佳木斯市", "08"))),
    (31, "上海市", True, (("上海市", "01"),)),
    (32, "江苏省", False, (("南京市", "01"), ("无锡市", "02"), ("徐州市", "03"),
                           ("常州市", "04"), ("苏州市", "05"), ("南通市", "06"),
                           ("连云港市", "07"), ("淮安市", "08"), ("盐城市", "09"),
                           ("扬州市", "10"))),
    (33, "浙江省", False, (("杭州市", "01"), ("宁波市", "02"), ("温州市", "03"),
                           ("嘉兴市", "04"), ("湖州市", "05"), ("绍兴市", "06"),
                           ("金华市", "07"), ("衢州市", "08"), ("舟山市", "09"),
                           ("台州市", "10"))),
    (34, "安徽省", False, (("合肥市", "01"), ("芜湖市", "02"), ("蚌埠市", "03"),
                           ("淮南市", "04"), ("马鞍山市", "05"), ("安庆市", "08"),
                           ("黄山市", "10"), ("滁州市", "11"), ("阜阳市", "12"))),
    (35, "福建省", False, (("福州市", "01"), ("厦门市", "02"), ("莆田市", "03"),
                           ("三明市", "04"), ("泉州市", "05"), ("漳州市", "06"),
                           ("南平市", "07"), ("龙岩市", "08"), ("宁德市", "09"))),
    (36, "江西省", False, (("南昌市", "01"), ("景德镇市", "02"), ("萍乡市", "03"),
                           ("九江市", "04"), ("新余市", "05"), ("鹰潭市", "06"),
                           ("赣州市", "07"), ("吉安市", "08"))),
    (37, "山东省", False, (("济南市", "01"), ("青岛市", "02"), ("淄博市", "03"),
                           ("枣庄市", "04"), ("东营市", "05"), ("烟台市", "06"),
                           ("潍坊市", "07"), ("济宁市", "08"), ("泰安市", "09"),
                           ("威海市", "10"), ("临沂市", "13"))),
    (41, "河南省", False, (("郑州市", "01"), ("开封市", "02"), ("洛阳市", "03"),
                           ("平顶山市", "04"), ("安阳市", "05"), ("鹤壁市", "06"),
                           ("新乡市", "07"), ("焦作市", "08"), ("濮阳市", "09"),
                           ("许昌市", "10"), ("南阳市", "13"))),
    (42, "湖北省", False, (("武汉市", "01"), ("黄石市", "02"), ("十堰市", "03"),
                           ("宜昌市", "05"), ("襄阳市", "06"), ("鄂州市", "07"),
                           ("荆门市", "08"), ("孝感市", "09"), ("荆州市", "10"))),
    (43, "湖南省", False, (("长沙市", "01"), ("株洲市", "02"), ("湘潭市", "03"),
                           ("衡阳市", "04"), ("邵阳市", "05"), ("岳阳市", "06"),
                           ("常德市", "07"), ("张家界市", "08"), ("益阳市", "09"))),
    (44, "广东省", False, (("广州市", "01"), ("韶关市", "02"), ("深圳市", "03"),
                           ("珠海市", "04"), ("汕头市", "05"), ("佛山市", "06"),
                           ("江门市", "07"), ("湛江市", "08"), ("茂名市", "09"),
                           ("肇庆市", "12"), ("惠州市", "13"))),
    (45, "广西壮族自治区", False, (("南宁市", "01"), ("柳州市", "02"), ("桂林市", "03"),
                                   ("梧州市", "04"), ("北海市", "05"), ("防城港市", "06"),
                                   ("钦州市", "07"), ("贵港市", "08"), ("玉林市", "09"))),
    (46, "海南省", False, (("海口市", "01"), ("三亚市", "02"), ("儋州市", "04"))),
    (50, "重庆市", True, (("重庆市", "01"),)),
    (51, "四川省", False, (("成都市", "01"), ("自贡市", "03"), ("攀枝花市", "04"),
                           ("泸州市", "05"), ("德阳市", "06"), ("绵阳市", "07"),
                           ("广元市", "08"), ("遂宁市", "09"), ("内江市", "10"),
                           ("乐山市", "11"))),
    (52, "贵州省", False, (("贵阳市", "01"), ("六盘水市", "02"), ("遵义市", "03"),
                           ("安顺市", "04"), ("毕节市", "05"))),
    (53, "云南省", False, (("昆明市", "01"), ("曲靖市", "03"), ("玉溪市", "04"),
                           ("保山市", "05"), ("昭通市", "06"), ("丽江市", "07"),
                           ("普洱市", "08"), ("临沧市", "09"))),
    (54, "西藏自治区", False, (("拉萨市", "01"), ("日喀则市", "02"), ("昌都市", "03"),
                               ("林芝市", "04"))),
    (61, "陕西省", False, (("西安市", "01"), ("铜川市", "02"), ("宝鸡市", "03"),
                           ("咸阳市", "04"), ("渭南市", "05"), ("延安市", "06"),
                           ("汉中市", "07"), ("榆林市", "08"))),
    (62, "甘肃省", False, (("兰州市", "01"), ("嘉峪关市", "02"), ("金昌市", "03"),
                           ("白银市", "04"), ("天水市", "05"), ("武威市", "06"),
                           ("张掖市", "07"), ("平凉市", "08"))),
    (63, "青海省", False, (("西宁市", "01"), ("海东市", "02"))),
    (64, "宁夏回族自治区", False, (("银川市", "01"), ("石嘴山市", "02"), ("吴忠市", "03"),
                                   ("固原市", "04"))),
    (65, "新疆维吾尔自治区", False, (("乌鲁木齐市", "01"), ("克拉玛依市", "02"),
                                     ("吐鲁番市", "04"), ("哈密市", "05"))),
)

#: 省级人口粗权重（决定"这个人落在哪个省"的分布，用常住人口量级即可，不必精确）
_POP_WEIGHTS = {
    11: 1.4, 12: 1.0, 13: 5.0, 14: 2.2, 15: 1.5, 21: 3.0, 22: 1.6, 23: 2.2,
    31: 1.8, 32: 5.0, 33: 4.0, 34: 4.2, 35: 3.2, 36: 3.0, 37: 7.5, 41: 6.5,
    42: 4.0, 43: 4.0, 44: 8.5, 45: 3.3, 46: 0.7, 50: 2.2, 51: 5.5, 52: 2.6,
    53: 3.8, 54: 0.25, 61: 2.6, 62: 1.8, 63: 0.4, 64: 0.5, 65: 1.9,
}

_STREET_PREFIXES = "人民 中山 解放 建设 文化 科技 和平 胜利 创新 长江 黄河 海滨 大学 体育 学府".split()
_STREET_TYPES = "路 街 大道 巷".split()
_ESTATE_PREFIX = "阳光 金色 翠湖 御景 龙湖 保利 万科 万达 香格里 天润".split()
_ESTATE_SUFFIX = "花园 小区 家园 公寓 大厦 广场".split()
_COMPANY_NAMES = "中瑞 华盛 远景 星辰 蓝海 卓越 飞扬 鼎盛 合众 博远 天启 晨光 汇通 开元 锦程".split()
_COMPANY_TRADE = "科技 信息咨询 贸易 文化传播 网络服务 机械制造 生物医药 环保工程 电子商务 食品".split()

#: 年龄段 → (下限, 上限, 权重)。分布刻意偏年轻但不至于全是 20 岁：真实网络问卷如此。
_AGE_BANDS = ((18, 24, 18), (25, 34, 30), (35, 44, 24), (45, 54, 16),
              (55, 64, 8), (65, 75, 4))

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK_CHARS = "10X98765432"

#: 默认随机源。刻意写成一个真 ``Random`` 实例而不是把 ``random`` 模块当实例传 ——
#: 后者 v2.x 就被 pyright 抓过一次（模块没有 choices/randrange 的类型签名，
#: `rng or random` 这种写法会把类型摊成 `Random | Module`，9 条错一起冒出来）。
_DEFAULT_RNG = random.Random()


def _pick_weighted(rng: random.Random, pairs: list[tuple[str, float]]) -> str:
    return rng.choices([p[0] for p in pairs], weights=[p[1] for p in pairs], k=1)[0]


@dataclass(frozen=True)
class Persona:
    """一个人的画像。所有派生字段在构造时就定下来，之后再读不会变。"""

    gender: str                 # "M" | "F"
    age: int
    birth_year: int
    birth_date: date            # 精确到日：身份证出生段与生日题必须同源（v4.2）
    education: str
    occupation: str
    income: str
    marital: str
    children: str
    province_code: int
    province: str
    city: str
    municipality: bool
    name: str
    id_card: str
    phone: str
    email: str
    company: str
    address_text: str

    # ---- 供各填空字段取用（同一份画像，绝不再抽第二次） ----
    def region(self, with_city: bool = True) -> str:
        """地区题（省 / 市级联框）用的串。"""
        return f"{self.province} {self.city}" if with_city else self.province

    def birth_date_text(self, fmt: str = "%Y-%m-%d") -> str:
        """生日题：与身份证中间 8 位**同一个日期**（v4.2 前各抽各的，月日对不上）。"""
        return self.birth_date.strftime(fmt)


# ============================================================================
#  派生细节
# ============================================================================
#: 前 20 大姓占汉族人口约八成半。整池等权抽会造出一堆现实里罕见的姓，也让下面那张
#: 拼音表大面积落空（邮箱前缀退化成 user123 一类），所以按 4:1 加权。
_SURNAMES_HEAVY = "王李张刘陈杨黄周吴徐孙马朱胡郭何林罗郑梁谢宋唐许韩冯邓曹"
_SURNAME_LIST = list(dict.fromkeys(_SURNAMES))
_SURNAME_WEIGHTS = [4.0 if s in _SURNAMES_HEAVY else 1.0 for s in _SURNAME_LIST]


def _surname(rng: random.Random) -> str:
    return rng.choices(_SURNAME_LIST, weights=_SURNAME_WEIGHTS, k=1)[0]


def _name(rng: random.Random, gender: str) -> str:
    surname = _surname(rng)
    pool = _GIVEN_NAMES_M if gender == "M" else _GIVEN_NAMES_F
    if rng.random() < 0.2:                     # 两成名字不分性别
        pool = pool + _GIVEN_NAMES_UNISEX
    given = rng.choice(pool)
    if len(given) > 1 and rng.random() < 0.35:  # 三成概率只用首字 → 2 字名
        given = given[0]
    return surname + given


def _id_card(rng: random.Random, region6: str, birth: str, gender: str) -> str:
    """6 位区划 + 8 位出生段 + 3 位顺序码 + ISO 7064 校验位。

    ``region6`` 由调用方用省码 + **真实 4 位市码**拼出（v4.2 起名录直存真码，
    不再由截断名录的下标算术推 —— 那条路会把临沂算到日照头上）。
    """
    seq = rng.randint(0, 99) * 10               # 前两位顺序码
    last = seq + (1 if gender == "M" else 2)    # 末位奇=男、偶=女（2 而非 0，避免全偶）
    body = f"{region6}{birth}{last:03d}"
    total = sum(int(ch) * w for ch, w in zip(body, _ID_WEIGHTS))
    return body + _ID_CHECK_CHARS[total % 11]


def _email(rng: random.Random, surname: str, gender: str) -> str:
    stem = _SURNAME_PINYIN.get(surname)
    if stem is None:
        stem = "user%03d" % rng.randint(0, 999)
    given_len = 1 if gender == "M" else 2
    local = stem + "".join(rng.choice("abcdefghijklmnopqrstuvwxyz")
                           for _ in range(given_len))
    if rng.random() < 0.5:
        local += str(rng.randint(1, 9999))
    return f"{local}@{rng.choice(_EMAIL_DOMAINS)}"


def _education(rng: random.Random, age: int) -> str:
    """学历按年龄段给条件分布：年轻一代本科率显著更高，55 岁以上以初中及以下为主。"""
    if age <= 24:
        table = [("高中", .20), ("大专", .30), ("本科", .45), ("硕士", .05)]
    elif age <= 34:
        table = [("高中", .15), ("大专", .28), ("本科", .47), ("硕士", .08), ("博士", .02)]
    elif age <= 44:
        table = [("初中", .08), ("高中", .22), ("大专", .35), ("本科", .29),
                 ("硕士", .05), ("博士", .01)]
    elif age <= 54:
        table = [("小学", .07), ("初中", .20), ("高中", .35), ("大专", .27),
                 ("本科", .09), ("硕士", .02)]
    elif age <= 64:
        table = [("小学", .15), ("初中", .35), ("高中", .30), ("大专", .13),
                 ("本科", .06), ("硕士", .01)]
    else:
        table = [("小学", .28), ("初中", .36), ("高中", .25), ("大专", .08),
                 ("本科", .03)]
    return _pick_weighted(rng, table)


def _occupation(rng: random.Random, age: int, education: str) -> str:
    if age >= 60:
        return "退休" if rng.random() < 0.75 else "个体经营"
    if age <= 24 and education in ("高中", "大专", "本科") and rng.random() < 0.5:
        return "学生"
    if education == "博士":
        return _pick_weighted(rng, [("高校教师", .35), ("科研人员", .3),
                                    ("医生", .2), ("公司职员", .15)])
    if education == "硕士":
        return _pick_weighted(rng, [("公司职员", .35), ("公务员", .15), ("医生", .1),
                                    ("教师", .2), ("IT 从业", .2)])
    if education == "本科":
        return _pick_weighted(rng, [("公司职员", .35), ("IT 从业", .15), ("教师", .12),
                                    ("公务员", .1), ("销售", .12), ("设计", .08),
                                    ("医护", .08)])
    if education == "大专":
        return _pick_weighted(rng, [("公司职员", .3), ("技术工人", .2), ("销售", .15),
                                    ("个体经营", .15), ("服务人员", .12), ("司机", .08)])
    return _pick_weighted(rng, [("工人", .3), ("个体经营", .22), ("农民", .18),
                                ("服务人员", .2), ("公司职员", .1)])


def _income(rng: random.Random, age: int, occupation: str) -> str:
    if occupation == "学生":
        return _pick_weighted(rng, [("3000 元以下", .55), ("3000-5000 元", .35),
                                    ("5000-8000 元", .1)])
    if occupation == "退休":
        return _pick_weighted(rng, [("3000 元以下", .35), ("3000-5000 元", .4),
                                    ("5000-8000 元", .2), ("8000-12000 元", .05)])
    low_first = occupation in ("农民", "服务人员", "工人", "司机")
    if age <= 26:
        base = [("3000 元以下", .3), ("3000-5000 元", .4), ("5000-8000 元", .22),
                ("8000-12000 元", .07), ("12000 元以上", .01)]
    elif age <= 44:
        base = [("3000 元以下", .1), ("3000-5000 元", .27), ("5000-8000 元", .32),
                ("8000-12000 元", .19), ("12000-20000 元", .09), ("20000 元以上", .03)]
    else:
        base = [("3000 元以下", .14), ("3000-5000 元", .28), ("5000-8000 元", .27),
                ("8000-12000 元", .17), ("12000-20000 元", .1), ("20000 元以上", .04)]
    if low_first and age > 26:                 # 同样工龄，这些职业的收入档整体下移一档
        base = [(b[0], b[1] * 1.6) for b in base[:4]] + base[4:]
    return _pick_weighted(rng, base)


def _marital(rng: random.Random, age: int) -> str:
    if age <= 21:
        married = .02
    elif age <= 25:
        married = .22
    elif age <= 29:
        married = .55
    elif age <= 39:
        married = .82
    else:
        married = .9
    return "已婚" if rng.random() < married else "未婚"


def _children(rng: random.Random, age: int, marital: str) -> str:
    if marital == "未婚":
        return "无"
    has = .15 if age <= 27 else .55 if age <= 39 else .8
    return _pick_weighted(rng, [("有", has), ("无", 1 - has)])


def _address(rng: random.Random, province: str, city: str, municipality: bool) -> str:
    """省市 + 街道门牌。直辖市不写两遍市名，自治区写全称。

    刻意在画像里**一次算好**：一份问卷里出现两道地址题时（很常见，一道"现居住地"
    一道"户籍地"），调用时才随机会长出两个住址。
    """
    head = city if municipality else f"{province}{city}"
    road = f"{rng.choice(_STREET_PREFIXES)}{rng.choice(_STREET_TYPES)}"
    text = f"{head}{road}{rng.randint(1, 999)}号"
    if rng.random() < 0.4:
        estate = f"{rng.choice(_ESTATE_PREFIX)}{rng.choice(_ESTATE_SUFFIX)}"
        text += f"{estate}{rng.randint(1, 30)}栋{rng.randint(101, 3209)}室"
    return text


def draw_persona(rng: random.Random | None = None) -> Persona:
    """抽一个人：先定地区与性别年龄，再让其余字段从这条链上派生。"""
    r: random.Random = rng if rng is not None else _DEFAULT_RNG
    prov_code = r.choices([p[0] for p in _REGIONS],
                          weights=[_POP_WEIGHTS[p[0]] for p in _REGIONS], k=1)[0]
    province, municipality, cities = next(
        (p[1], p[2], p[3]) for p in _REGIONS if p[0] == prov_code
    )
    city_name, city_code = cities[r.randrange(len(cities))]
    city = city_name

    gender = "M" if r.random() < 0.51 else "F"
    band = r.choices([(b[0], b[1]) for b in _AGE_BANDS],
                     weights=[b[2] for b in _AGE_BANDS], k=1)[0]
    age = r.randint(band[0], band[1])
    birth_year = date.today().year - age

    education = _education(r, age)
    occupation = _occupation(r, age, education)
    income = _income(r, age, occupation)
    marital = _marital(r, age)
    children = _children(r, age, marital)

    surname_name = _name(r, gender)
    # 出生月日照真实月历抽（此前恒 ≤28 号，29/30/31 从不出现，分布可被统计识别）
    birth_month = r.randint(1, 12)
    birth_day = r.randint(1, calendar.monthrange(birth_year, birth_month)[1])
    birth_date = date(birth_year, birth_month, birth_day)
    return Persona(
        gender=gender,
        age=age,
        birth_year=birth_year,
        birth_date=birth_date,
        education=education,
        occupation=occupation,
        income=income,
        marital=marital,
        children=children,
        province_code=prov_code,
        province=province,
        city=city,
        municipality=municipality,
        name=surname_name,
        id_card=_id_card(r, f"{prov_code}{city_code}00",
                         birth_date.strftime("%Y%m%d"), gender),
        phone=f"{r.choice(_MOBILE_PREFIXES)}{r.randint(0, 99_999_999):08d}",
        email=_email(r, surname_name[0], gender),
        company=(f"{city[:-1] if city.endswith('市') else city}"
                 f"{r.choice(_COMPANY_NAMES)}{r.choice(_COMPANY_TRADE)}有限公司"),
        address_text=_address(r, province, city, municipality),
    )


# ============================================================================
#  本次提交用哪个人
# ============================================================================
_local = threading.local()


def new_persona() -> Persona:
    """换一个人：每次提交开始时调用一次，之后所有填空都读这一份。

    放在 thread-local 上而不是模块全局：GUI 的批次跑在 worker 线程里，
    测试又常在主线程直接调 ``generate_answer`` —— 共用一个全局会让两边互相看见
    对方的画像，症状是"测试改不动生产行为"或反过来。
    """
    p = draw_persona()
    _local.persona = p
    return p


def active_persona() -> Persona:
    """当前画像；没人换过就现场抽一个（保证任何调用点都有定义）。"""
    p: Persona | None = getattr(_local, "persona", None)
    if p is None:
        p = new_persona()
    return p
