"""Goal-focused DOM extraction. Keep cards, headings and table rows together."""
import re
from datetime import datetime, timezone
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from athena.tools._http import validate_url

_COMMON = set('the a an is are was were who what when where how which please tell me about of for in on at and or to from with latest current official results result schedule race event page information details today yesterday tomorrow grand prix formula f1'.split())

def _terms(query):
    return {word for word in re.findall(r'[\w]+', query.casefold()) if word not in _COMMON and not word.isdigit()}

def extract(html, url, max_chars=12000, query=''):
    soup = BeautifulSoup(html, 'html.parser')
    title = soup.title.get_text(' ', strip=True) if soup.title else url
    # Tabs such as "Results" may live in navigation that is removed from the
    # readable body. Keep their validated links without treating nav as facts.
    navigation = []
    for tag in soup.select('nav a[href], header a[href]'):
        target = urljoin(url, tag.get('href', ''))
        try:
            validate_url(target)
        except ValueError:
            continue
        navigation.append({'title': tag.get_text(' ', strip=True)[:300], 'url': target})
    for tag in soup.select('script,style,noscript,nav,footer,header,aside,form,svg,[hidden]'):
        tag.decompose()
    content = soup.find('main') or soup.find('article') or soup.body or soup
    terms = _terms(query)
    def score(text):
        words = set(re.findall(r'[\w]+', text.casefold()))
        return len(terms & words)
    def link(tag):
        target = urljoin(url, tag.get('href', ''))
        try:
            validate_url(target)
        except ValueError:
            return None
        return {'title': tag.get_text(' ', strip=True)[:300], 'url': target}

    all_links = [item for tag in content.find_all('a', href=True) if (item := link(tag))] + navigation
    # Rank before limiting: a relevant detail link may be the 40th calendar card.
    if terms:
        all_links.sort(key=lambda row: score(row['title']+' '+row['url']), reverse=True)
        relevant_links = [row for row in all_links if score(row['title']+' '+row['url'])]
        # Unrelated card titles often contain their entire podium. Do not leak
        # those facts back through the link list after excluding their sections.
        if relevant_links:
            all_links = relevant_links
    links, seen = [], set()
    for item in all_links:
        if item['url'] not in seen:
            links.append(item); seen.add(item['url'])
        if len(links) >= 20:
            break

    blocks, signatures = [], set()
    def add(tag, kind, heading='', target=''):
        text = tag.get_text(' ', strip=True)
        if not text or text in signatures:
            return
        signatures.add(text)
        row = {'kind': kind, 'heading': heading[:300], 'text': text,
               'source_url': target or url}
        if kind == 'table':
            row['rows'] = [[cell.get_text(' ', strip=True) for cell in tr.find_all(['th','td'], recursive=False)]
                           for tr in tag.find_all('tr')][:100]
            row['text'] = '\n'.join('ROW '+str(i+1)+' | '+' | '.join(cells)
                                    for i, cells in enumerate(row['rows']) if cells)
        blocks.append(row)

    # Repeated linked cards are common on calendars and product listings.
    # Retaining each full anchor prevents names/positions crossing card boundaries.
    for tag in content.find_all('a', href=True):
        if len(tag.get_text(' ', strip=True)) >= 45 and tag.find(['div','p','h2','h3','span']):
            item = link(tag)
            if item:
                add(tag, 'linked_card', tag.get_text(' ', strip=True)[:180], item['url'])
    # Heading-scoped sections; never take a whole multi-topic ancestor as a match.
    heading = title
    for tag in content.find_all(['h1','h2','h3','h4','p','li','table']):
        if tag.name in {'h1','h2','h3','h4'}:
            heading = tag.get_text(' ', strip=True)
        elif not tag.find_parent(['a','table']) and not (tag.name == 'li' and tag.find('a')):
            add(tag, 'table' if tag.name == 'table' else 'paragraph', heading)
    if not blocks:
        # A plain/div-only page cannot be reliably segmented. Explicitly label
        # the fallback rather than pretending a topic match proves relevance.
        add(content, 'unsegmented', title)
    matched = [row for row in blocks if not terms or score(row['heading']+' '+row['text'])]
    if terms:
        matched.sort(key=lambda row: score(row['heading']+' '+row['text']), reverse=True)
    selected, remaining, omitted = [], max_chars, False
    for row in matched[:40]:
        if remaining < 100:
            omitted = True; break
        row = dict(row)
        full = row['text']
        row['text'] = full[:remaining]
        row['truncated'] = len(full) > remaining
        remaining -= len(row['text']) + len(row['heading']) + 50
        selected.append(row)
        omitted |= row['truncated']
    omitted |= len(matched) > len(selected)
    text = '\n\n'.join(f"[SECTION {i+1}: {row['kind']} | {row['heading']}]\n{row['text']}\n[END SECTION {i+1}]"
                        for i, row in enumerate(selected))
    return {'title': title[:300], 'url': url, 'query': query,
            'text': text[:max_chars],
            'sections': [{key: value for key, value in row.items() if key not in {'text', 'rows'}} for row in selected],
            'links': links,
            'focus_matched': bool(matched), 'truncated': omitted or len(text)>max_chars,
            'omitted_sections': len(blocks)-len(selected),
            'retrieved_at': datetime.now(timezone.utc).isoformat(), 'untrusted_content': True,
            'evidence_note': 'Sections describe separate source items. A topic match is not proof that the requested fact exists. Do not borrow facts from another section. If missing, follow relevant links or search a more specific source.'}
