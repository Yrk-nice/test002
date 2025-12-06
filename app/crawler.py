import urllib.parse
import requests
import time
import random
import os
import re
from bs4 import BeautifulSoup
try:
    from lxml import html as lxml_html
except Exception:
    lxml_html = None


BASE_URL = 'https://www.baidu.com/s'


HEADERS = {
    'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    'accept-encoding': 'gzip, deflate',
    'accept-language': 'zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7',
    'connection': 'keep-alive',
    'sec-ch-ua': '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Windows"',
    'sec-fetch-dest': 'document',
    'sec-fetch-mode': 'navigate',
    'sec-fetch-site': 'same-origin',
    'sec-fetch-user': '?1',
    'upgrade-insecure-requests': '1',
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36',
}


def _clean(t):
    if not t:
        return ''
    return ' '.join(t.split())


def crawl_baidu_news(keyword: str, limit: int = 10, delay_ms: int | None = None):
    params = {
        'rtt': '1',
        'bsst': '1',
        'cl': '2',
        'tn': 'news',
        'ie': 'utf-8',
        'word': keyword,
    }
    headers = dict(HEADERS)
    headers['referer'] = f'https://www.baidu.com/s?&wd={urllib.parse.quote(keyword)}'
    if os.environ.get('BAIDU_COOKIE'):
        headers['cookie'] = os.environ['BAIDU_COOKIE']

    if delay_ms is None:
        env_delay = os.environ.get('CRAWL_DELAY_MS')
        if env_delay:
            try:
                delay_ms = int(env_delay)
            except Exception:
                delay_ms = None

    if delay_ms and delay_ms > 0:
        jitter = int(delay_ms * random.uniform(0.8, 1.2))
        time.sleep(jitter / 1000.0)

    resp = None
    backoff_ms = max(300, (delay_ms or 500))
    for attempt in range(3):
        try:
            resp = requests.get(BASE_URL, params=params, headers=headers, timeout=15)
            if resp.status_code in (429, 503):
                raise requests.HTTPError(f"status {resp.status_code}")
            break
        except Exception:
            if attempt < 2:
                time.sleep(((attempt + 1) * backoff_ms) / 1000.0)
                continue
            else:
                return []
    resp.raise_for_status()
    html = resp.text
    soup = BeautifulSoup(html, 'html.parser')
    items = []

    # 兼容不同结构：h3.t > a、a.news-title_1、div.result中包含标题链接
    blocks = []
    blocks.extend(soup.select('div.result'))
    blocks.extend(soup.select('div.news-card'))
    blocks.extend(soup.select('article'))
    blocks = blocks or soup.select('div')

    seen_urls = set()
    for block in blocks:
        a = None
        h3 = block.find('h3')
        if h3:
            a = h3.find('a')
        if not a:
            a = block.find('a', class_='news-title_1') or block.find('a', class_='c-title')
        if not a or not a.get('href'):
            continue
        title = _clean(a.get_text())
        url = (a.get('href') or '').strip()
        if not title or not url:
            continue
        if not (url.startswith('http://') or url.startswith('https://')):
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        src = None
        summ = None
        cover = None

        for cand in [
            block.find('span', class_='c-author'),
            block.find('span', class_='source'),
            block.find('div', class_='news-source'),
            block.find('p', class_='source'),
            block.find('span', class_='c-source'),
            block.find('a', class_='c-showurl'),
        ]:
            if cand and _clean(cand.get_text()):
                src = _clean(cand.get_text())
                break
        
        if not src and url:
            try:
                src = urllib.parse.urlparse(url).netloc
            except Exception:
                pass

        for cand in [
            block.find('div', class_='c-summary'),
            block.find('span', class_='c-abstract'),
            block.find('div', class_='news-desc'),
            block.find('p'),
        ]:
            if cand and _clean(cand.get_text()):
                summ = _clean(cand.get_text())
                break

        img = block.find('img')
        if img:
            cover = img.get('src') or img.get('data-src') or img.get('data-ori')

        items.append({
            'title': title,
            'summary': summ or '',
            'cover': cover or '',
            'url': url,
            'source': src or '',
        })
        if len(items) >= limit:
            break

    return items


def _decode_content(resp):
    raw = resp.content
    head = raw[:4096].lower()
    m = re.search(br"charset=([\w-]+)", head) or re.search(br"<meta[^>]*charset=['\"]?([\w-]+)", head)
    enc = None
    if m:
        enc = m.group(1).decode('ascii', errors='ignore')
    if not enc:
        enc = (resp.encoding or getattr(resp, 'apparent_encoding', None)) or 'utf-8'
    enc = enc.lower()
    if enc in ('gb2312', 'gbk'):
        enc = 'gb18030'
    try:
        text = raw.decode(enc, errors='ignore')
    except Exception:
        text = raw.decode('utf-8', errors='ignore')
    bad = text.count('\ufffd')
    if len(text) and bad / len(text) > 0.02:
        for alt in ('gb18030', 'utf-8', 'big5'):
            try:
                t2 = raw.decode(alt, errors='ignore')
                if t2.count('\ufffd') / max(1, len(t2)) < bad / max(1, len(text)):
                    text = t2
                    break
            except Exception:
                continue
    return text


def _extract_main_text(html: str) -> str:
    soup = BeautifulSoup(html, 'html.parser')
    sels = [
        'article',
        'div.article',
        '#article',
        'div.content',
        '#content',
        'section',
        'div.main',
        '#main',
        'div.post',
        '#post',
        'div.detail',
    ]
    best = ''
    for sel in sels:
        el = soup.select_one(sel)
        if el:
            t = el.get_text(' ', strip=True)
            if len(t) > len(best):
                best = t
    if not best:
        ps = soup.select('p')
        parts = []
        for p in ps:
            txt = p.get_text(' ', strip=True)
            if txt:
                parts.append(txt)
        best = ' '.join(parts)
    return (best or '')[:20000]


def deep_fetch_text(url: str) -> str:
    headers = dict(HEADERS)
    r = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
    r.raise_for_status()
    html = _decode_content(r)
    return _extract_main_text(html)


def _sanitize_headers_for_url(url: str, headers: dict) -> dict:
    h = dict(headers or {})
    enc = h.get('accept-encoding') or h.get('Accept-Encoding')
    if enc and ('br' in enc or 'zstd' in enc):
        h['accept-encoding'] = 'gzip, deflate'
    if not (h.get('user-agent') or h.get('User-Agent')):
        h['user-agent'] = HEADERS['user-agent']
    if not (h.get('referer') or h.get('Referer')):
        h['referer'] = url
    return h


def _extract_baijiahao_text(html: str) -> str | None:
    soup = BeautifulSoup(html, 'html.parser')
    for sel in ['#content', 'div.content', 'div.article', '#article', 'div.article-content', 'section']:
        el = soup.select_one(sel)
        if el:
            t = el.get_text(' ', strip=True)
            if t:
                return t
    return None


def deep_fetch_with_rule(url: str, headers_override: dict | None, title_xpath: str | None, content_xpath: str | None):
    headers = dict(HEADERS)
    if headers_override:
        headers.update(headers_override)
    headers = _sanitize_headers_for_url(url, headers)
    r = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
    r.raise_for_status()
    text = _decode_content(r)
    if not lxml_html:
        # 特殊站点回退
        if 'baijiahao.baidu.com' in url:
            bx = _extract_baijiahao_text(text) or ''
            return None, bx or _extract_main_text(text)
        return None, _extract_main_text(text)
    doc = lxml_html.fromstring(text)
    title = None
    content = None
    try:
        if title_xpath:
            ts = doc.xpath(title_xpath)
            if ts:
                t0 = ts[0]
                title = t0 if isinstance(t0, str) else t0.text_content()
        if content_xpath:
            cs = doc.xpath(content_xpath)
            if cs:
                ctexts = []
                for c in cs:
                    ctexts.append(c if isinstance(c, str) else c.text_content())
                content = '\n'.join(ctexts)
    except Exception:
        pass
    if not content:
        if 'baijiahao.baidu.com' in url:
            content = _extract_baijiahao_text(text) or ''
        content = content or _extract_main_text(text)
    return (title or '').strip(), (content or '').strip()


def fetch_page_text(url: str, headers_override: dict | None = None) -> str:
    headers = dict(HEADERS)
    if headers_override:
        headers.update(headers_override)
    headers = _sanitize_headers_for_url(url, headers)
    r = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
    r.raise_for_status()
    return _decode_content(r)


def suggest_content_xpath(page_text: str) -> str | None:
    if not lxml_html:
        return None
    try:
        doc = lxml_html.fromstring(page_text)
    except Exception:
        return None
    # 优先基于 id
    for i in ['content', 'article', 'main', 'post', 'detail']:
        el = doc.xpath(f'//*[@id="{i}"]')
        if el:
            return f'//*[@id="{i}"]'
    # 再基于 class contains
    for c in ['article-content', 'content', 'detail', 'post', 'text']:
        el = doc.xpath(f'//*[contains(@class,"{c}")]')
        if el:
            return f'//*[contains(@class,"{c}")]'
    return None
