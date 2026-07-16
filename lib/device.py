"""从 User-Agent 粗判设备类型与系统(仅用于用量统计,非安全用途)。

UA 可伪造,但普通浏览器都如实上报,用于"业务用手机还是电脑"的统计足够。
返回 {'device': '手机'/'平板'/'电脑'/'未知', 'os': 'iOS'/'Android'/'Windows'/'Mac'/'其他'}。
"""


def parse_ua(ua: str) -> dict:
    s = (ua or '').lower()
    if not s:
        return {'device': '未知', 'os': '其他'}

    # 系统
    if 'iphone' in s or 'ipod' in s:
        os_name = 'iOS'
    elif 'ipad' in s:
        os_name = 'iPadOS'
    elif 'android' in s:
        os_name = 'Android'
    elif 'windows' in s:
        os_name = 'Windows'
    elif 'mac os' in s or 'macintosh' in s:
        os_name = 'Mac'
    elif 'linux' in s:
        os_name = 'Linux'
    else:
        os_name = '其他'

    # 设备类型:先判平板,再判手机,否则电脑
    is_tablet = ('ipad' in s
                 or ('android' in s and 'mobile' not in s)   # Android 平板 UA 不含 mobile
                 or 'tablet' in s)
    is_mobile = ('iphone' in s or 'ipod' in s
                 or ('android' in s and 'mobile' in s)
                 or 'windows phone' in s
                 or ('mobile' in s and 'ipad' not in s))
    if is_tablet:
        device = '平板'
    elif is_mobile:
        device = '手机'
    else:
        device = '电脑'
    return {'device': device, 'os': os_name}
