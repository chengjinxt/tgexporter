from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class SourceSiteRule:
    domain: str
    name: str
    resource_method: str
    fallback_method: str
    notes: str
    link_display: str = "正文中显示来源名称和完整地址。"
    login_requirement: str = "通常无需登录"
    login_hint: str = ""


SOURCE_SITE_RULES: dict[str, SourceSiteRule] = {
    "weibo.com": SourceSiteRule(
        domain="weibo.com",
        name="微博 / 新浪科技",
        resource_method="浏览器渲染后优先截取微博正文媒体；无媒体时截取单条微博正文区域",
        fallback_method="Google 图片搜索同标题相似配图",
        notes="匿名 HTTP 通常跳转到 Visitor System，不能只靠 urllib 抓图。",
        login_requirement="可能需要登录",
        login_hint="如遇 Visitor System 或看不到图片，先运行 source-login 登录微博。",
    ),
    "m.weibo.cn": SourceSiteRule(
        domain="m.weibo.cn",
        name="微博移动页",
        resource_method="浏览器渲染后优先截取微博正文媒体；无媒体时截取单条微博正文区域",
        fallback_method="Google 图片搜索同标题相似配图",
        notes="移动页匿名访问也可能进入 Visitor System。",
        login_requirement="可能需要登录",
        login_hint="如遇 Visitor System 或看不到图片，先运行 source-login 登录微博。",
    ),
    "sohu.com": SourceSiteRule(
        domain="sohu.com",
        name="搜狐网",
        resource_method="优先 OpenGraph / 正文 img；无合格正文图时截图文章正文区域",
        fallback_method="Google 图片搜索同标题相似配图",
        notes="部分文章的 og:image 是搜狐 Logo 或二维码，需要过滤。",
    ),
    "bloomberg.com": SourceSiteRule(
        domain="bloomberg.com",
        name="Bloomberg",
        resource_method="浏览器渲染后截取最大新闻图",
        fallback_method="Google 图片搜索同标题相似配图",
        notes="普通 HTTP 可能 403 或遇到订阅遮罩，需用浏览器 DOM/截图兜底。",
    ),
    "qbitai.com": SourceSiteRule(
        domain="qbitai.com",
        name="量子位",
        resource_method="正文 img；下载图片时带原文 Referer",
        fallback_method="浏览器截图或 Google 图片搜索",
        notes="og:image 可能是站点 Logo，正文图通常在 i.qbitai.com。",
    ),
    "ithome.com": SourceSiteRule(
        domain="ithome.com",
        name="IT之家",
        resource_method="优先过滤 OpenGraph 占位图；浏览器渲染后提取 newsuploadfiles 正文新闻图 URL 下载",
        fallback_method="浏览器截图或 Google 图片搜索",
        notes="部分文章的 og:image 是 images/v2/t.png 占位图，必须跳过后再取正文图。",
    ),
    "wsj.com": SourceSiteRule(
        domain="wsj.com",
        name="Wall Street Journal",
        resource_method="浏览器渲染后提取 images.wsj.net 正文新闻图 URL；若出现验证页则停止截图并提示 source-login",
        fallback_method="source-login 完成人机验证后重试；仍失败则 Google 图片搜索同标题相似配图",
        notes="匿名或自动浏览器可能看到“访问暂时受限 / 我不是机器人”页面，不能把验证页截图当文章图。",
        login_requirement="可能需要登录或人机验证",
        login_hint="如果看到 WSJ 访问受限或机器人验证页，先运行 source-login 完成验证。",
    ),
    "reuters.com": SourceSiteRule(
        domain="reuters.com",
        name="Reuters",
        resource_method="OpenGraph / 正文 img；失败时浏览器渲染后提取正文新闻图 URL 下载",
        fallback_method="浏览器截图或 Google 图片搜索",
        notes="页面结构可能随地区和订阅提示变化；遇到空白页、401 或访问受限时需要 source-login 完成人机验证。",
        login_requirement="可能需要人机验证",
        login_hint="如果抓到 Reuters 访问受限页，先运行 source-login 完成人机验证。",
    ),
    "finance.sina.com.cn": SourceSiteRule(
        domain="finance.sina.com.cn",
        name="新浪财经",
        resource_method="OpenGraph / 正文 img；无配图时截取无广告正文内容或搜索相关配图/公司Logo",
        fallback_method="截取纯净正文或 Google 图片搜索同标题配图及公司Logo",
        notes="新浪系页面可能有防盗链，下载图片时优先携带 Referer；正文底部常有黑猫投诉等广告轮播，严禁作为文章配图。",
    ),
    "fifa.com": SourceSiteRule(
        domain="fifa.com",
        name="FIFA",
        resource_method="浏览器渲染后提取正文图片 URL，再带 Referer 下载 digitalhub.fifa.com 原图",
        fallback_method="浏览器截取正文大图；仍失败才进入 Google 图片搜索",
        notes="FIFA 文章首屏 HTML 可能没有 og:image 或 img，需要等待前端渲染并滚动触发懒加载。",
        login_requirement="通常无需登录",
    ),
    "axios.com": SourceSiteRule(
        domain="axios.com",
        name="Axios",
        resource_method="浏览器渲染后提取正文主图 URL，再带 Referer 下载 images.axios.com 图片",
        fallback_method="浏览器截取正文主图；仍失败才进入 Google 图片搜索",
        notes="Axios 匿名 HTTP 可能返回 403 或 Cloudflare 安全验证，通过 source-login 完成验证后再复用浏览器 profile 抓图。",
        login_requirement="可能需要人机验证",
        login_hint="如果看到 Cloudflare 验证页，先运行 source-login 打开 Axios 并完成验证。",
    ),
    "x.com": SourceSiteRule(
        domain="x.com",
        name="X / Twitter",
        resource_method="浏览器渲染后截取正文媒体；无媒体时截取单条 Post 正文区域",
        fallback_method="Google 图片搜索同标题相似配图",
        notes="匿名页面经常受登录墙影响，直接 HTTP 抓取通常不可依赖。",
        login_requirement="可能需要登录",
        login_hint="如果 X 页面提示登录或无法查看 Post，先运行 source-login 登录 X。",
    ),
    "mp.weixin.qq.com": SourceSiteRule(
        domain="mp.weixin.qq.com",
        name="微信公众号文章",
        resource_method="保留为正文超链接，并从页面提取 mmbiz.qpic.cn 正文图片作为配图",
        fallback_method="浏览器截图正文或 Google 图片搜索",
        notes="该域名不进入普通引用来源统计，也不输出到文末引用区。",
        link_display="正文中显示为公众号超链接。",
        login_requirement="通常无需登录",
    ),
    "theinformation.com": SourceSiteRule(
        domain="theinformation.com",
        name="The Information",
        resource_method="浏览器关闭订阅弹窗后提取 tii.imgix.net 新闻主图 URL 下载",
        fallback_method="截图文章正文区域或 Google 图片搜索同标题相似配图",
        notes="页面常见订阅弹窗或付费墙，需先关闭弹窗再取图；普通 HTTP 元数据可能为空。",
        login_requirement="可能需要登录或订阅",
        login_hint="如果只能看到订阅弹窗或付费墙，先运行 source-login 登录 The Information。",
    ),
    "cls.cn": SourceSiteRule(
        domain="cls.cn",
        name="科创板日报 / 财联社",
        resource_method="优先正文 img；无文章图时截图正文内容区域",
        fallback_method="Google 图片搜索同标题相似配图",
        notes="部分文章只有正文文字，无配图时截图正文首屏，避开页面头部和广告。",
        login_requirement="通常无需登录",
    ),
    "moe.gov.cn": SourceSiteRule(
        domain="moe.gov.cn",
        name="教育部官网",
        resource_method="解析列表页与正文页，提取标题、发布日期、正文段落和正文图片",
        fallback_method="无正文配图时生成春晖学府教育资讯封面",
        notes="适合春晖学府定时采集权威教育政策、教师培养、资助和升学相关信息。",
        login_requirement="通常无需登录",
    ),
}


def source_rule_for_url(url: str) -> SourceSiteRule | None:
    hostname = (urlparse(url).hostname or "").lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    for domain in sorted(SOURCE_SITE_RULES, key=len, reverse=True):
        if hostname == domain or hostname.endswith(f".{domain}"):
            return SOURCE_SITE_RULES[domain]
    return None
