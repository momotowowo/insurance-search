"""Build exact-version rider indexes from cloud-browser TII observations.

Usage: python scripts/build_rider_shard.py SLUG EVIDENCE_JSON YYYY-MM-DD
Only observed M<->R/A pairs are accepted. Both endpoints must be verified
currently on sale. No name, product-family, or successor-version matching.
"""
import base64
import collections
import gzip
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

ROOT = Path(__file__).resolve().parents[1]
COMPANIES = {'global': '全球人壽', 'yuanta': '元大人壽', 'tcb': '合作金庫人壽',
             'aia': '友邦人壽', 'bot': '臺銀人壽', 'post': '中華郵政簡易壽險'}


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def tid(url):
    return parse_qs(urlparse(url or '').query).get('productId', [''])[0]


def roc_date(value):
    m = re.fullmatch(r'(\d{2,3})/(\d{1,2})/(\d{1,2})', value)
    return f'{int(m[1])+1911:04d}-{int(m[2]):02d}-{int(m[3]):02d}' if m else value


def field(text, label):
    m = re.search(re.escape(label) + r'\s*([^\n]+)', text)
    return m[1].strip() if m else ''


def key(p):
    return dumps([p.get('insurer', ''), p.get('name', ''), p.get('code', ''), p.get('contract_type', '')])


def refresh_data_release():
    paths = [ROOT / 'data/riders/manifest.js', ROOT / 'data/riders/product_updates.js']
    release = 'riders-' + hashlib.sha256(b''.join(p.read_bytes() for p in paths)).hexdigest()[:16]
    path = ROOT / 'index.html'
    html, count = re.subn(r"const release = '[^']+';", f"const release = '{release}';", path.read_text())
    assert count == 1, 'missing or ambiguous data release marker'
    path.write_text(html)


def build(slug, evidence_path, date):
    co = COMPANIES[slug]
    raw = json.loads(Path(evidence_path).read_text())
    pages = {}
    for e in raw:
        assert e['code'] == tid(e['url']), 'source URL/code mismatch'
        assert '保險商品詳細資料' in e['text'], 'not a detail page'
        stop = field(e['text'], '停售日：')
        sale = field(e['text'], '銷售日：')
        name = e['text'].split('保險商品詳細資料', 1)[1].split('銷售日：', 1)[0].strip()
        assert name and stop and sale
        p = {'code': e['code'], 'url': e['url'], 'name': name, 'sale_date': roc_date(sale),
             'stop_date': roc_date(stop), 'has_additional_field': e['hasAdditional'],
             'rows': e['rows']}
        if e.get('terms'):
            p['terms_url'] = urljoin(e['url'], e['terms'])
        if e['code'] in pages:
            assert pages[e['code']]['rows'] == p['rows'], 'conflicting duplicate observations'
        pages[e['code']] = p
    live = {c: p for c, p in pages.items() if p['stop_date'] == '未停售' and p['sale_date'] != '未銷售'}
    packed = (ROOT / 'data/products-packed.js').read_text()
    payload = json.loads(gzip.decompress(base64.b64decode(packed.split('"')[1])))
    products, meta = payload['products'], payload['meta']
    overlay_path = ROOT / 'data/riders/product_updates.json'
    overlay = json.loads(overlay_path.read_text()) if overlay_path.exists() else {'products': []}
    original_products = {p['id']: dict(p) for p in products}
    merged = {p['id']: p for p in products}
    merged.update({p['id']: p for p in overlay['products']})
    products = list(merged.values())
    if overlay.get('meta'):
        meta = overlay['meta']
    company_products = [p for p in products if p.get('insurer') == co]
    by_tid = collections.defaultdict(list)
    for p in company_products:
        if tid(p.get('tii_url')):
            by_tid[tid(p.get('tii_url'))].append(p)
    next_id = max(int(p['id']) for p in products) + 1
    added, corrections = [], []
    for code, e in pages.items():
        if not by_tid[code] and code in live:
            p = {'name': e['name'], 'insurer': co, 'original_insurer': co, 'current_insurer': co,
                 'sector': '人身保險', 'category': '附約' if code[6] in 'RA' else '未分類',
                 'status': '現售', 'sale_date': e['sale_date'], 'stop_date': e['stop_date'],
                 'tii_url': e['url'], 'source': '保發中心', 'source_name': '保發中心商品詳細頁',
                 'id': str(next_id).zfill(5), 'first_seen_at': date+'T00:00:00+08:00',
                 'first_seen_source': 'discovered', 'rider_source_checked_at': date}
            if e.get('terms_url'):
                p['terms_url'] = e['terms_url']
            products.append(p)
            by_tid[code].append(p)
            added.append(p['id'])
            next_id += 1
        for p in by_tid[code]:
            status = '現售' if code in live else ('停售' if re.fullmatch(r'\d{4}-\d{2}-\d{2}', e['stop_date']) else p['status'])
            if p['status'] != status or p.get('stop_date') != e['stop_date']:
                corrections.append({'id': p['id'], 'code': code, 'before': {'status': p['status'], 'stop_date': p.get('stop_date')},
                                    'after': {'status': status, 'stop_date': e['stop_date']}})
                p.update(status=status, stop_date=e['stop_date'], rider_source_checked_at=date)
    pairs = collections.defaultdict(list)
    excluded = []
    for code, e in pages.items():
        for row in e['rows']:
            cells = row['cells']
            if not cells or cells == ['無']:
                continue
            if len(cells) != 2 or not re.fullmatch(r'\d{6}[MRAE][A-Za-z0-9]+', cells[1]):
                excluded.append({'from': code, 'cells': cells, 'reason': 'unparseable_exact_code'})
                continue
            target = cells[1]
            # Links must agree with the displayed identifier. Plain text is
            # evidence too, but is never used to guess another identifier.
            link_codes = {tid(a['url']) for a in row['links'] if tid(a['url'])}
            if link_codes and link_codes != {target}:
                excluded.append({'from': code, 'to': target,
                                 'link_codes': sorted(link_codes), 'reason': 'link_code_mismatch'})
                continue
            if code[6] == 'M' and target[6] in 'RA':
                pair = (code, target)
            elif code[6] in 'RA' and target[6] == 'M':
                pair = (target, code)
            else:
                continue  # endorsements or rider-to-rider links are not main/rider pairs
            if any(c not in live for c in pair):
                excluded.append({'from': code, 'to': target, 'reason': 'not_both_verified_on_sale'})
                continue
            assert all(by_tid[c] for c in pair)
            assert all(p['insurer'] == co for c in pair for p in by_tid[c])
            if code not in pairs[pair]:
                pairs[pair].append(code)
    sl = '保發中心，依各公司現行核保規定辦理'
    note = '僅依保發中心已查核之現售主附約版本建立關聯；實際附加依核保規定。'
    shard = {'v': 3, 'co': co, 'slug': slug, 'sl': sl, 'mt': {}, 'mk': {}, 'mp': {},
             'rt': {}, 'rk': {}, 'rb': {}, 'bfs': {}, 'um': [], 'os': 0, 'rn': note}
    def product(c):
        return next(p for p in by_tid[c] if p['status'] == '現售')
    def entry(c, direction):
        p = product(c)
        return {'n': p['name'], 't': c, 'k': key(p), 'id': p['id'], 'vd': date, direction: []}
    def link(c, source):
        p = product(c)
        return {'n': p['name'], 'c': c, 'on': 1, 'id': p['id'], 'st': '現售', 'es': source}
    for (m, r), sources in sorted(pairs.items()):
        shard['mt'].setdefault(m, entry(m, 'r'))['r'].append(link(r, 'official_tii' if m in sources else 'official_tii_reverse'))
        shard['rt'].setdefault(r, entry(r, 'm'))['m'].append(link(m, 'official_tii' if r in sources else 'rev'))
    manifest = json.loads((ROOT / 'data/riders/manifest.json').read_text())
    for section in ('main', 'rider'):
        manifest[section] = {k: v for k, v in manifest[section].items() if v['co'] != co}
    for section, table, direction in [('main', 'mt', 'r'), ('rider', 'rt', 'm')]:
        for c, e in shard[table].items():
            md = {'co': co, 'n': len(e[direction])}
            manifest[section]['t:'+c] = md
            for p in by_tid[c]:
                if p['status'] != '現售':
                    continue
                manifest[section]['k:'+key(p)] = dict(md, t=c)
                if section == 'main':
                    shard['mk'][key(p)] = c
                    shard['mp'][p['id']] = c
                else:
                    # Exact full name only. No cross-version substitution.
                    manifest[section]['b:'+co+'|'+p['name']] = dict(md, t=c)
                    shard['rk'][key(p)] = c
                    shard['rb'][co+'|'+p['name']] = c
    summary = {'checked_pages': len(pages), 'verified_on_sale_pages': len(live),
               'main_rider_pairs': len(pairs), 'mains_with_riders': len(shard['mt']),
               'riders_with_mains': len(shard['rt']), 'new_products': len(added),
               'status_corrections': len(corrections), 'excluded_rows': len(excluded), 'verified_date': date}
    shard['bfs'] = {'pages': len(pages), 'official_exact_edges': len(pairs), 'onsale_only': True,
                    'all_site_sources': False, 'scope': 'site_on_sale_products_and_linked_versions',
                    'matched_link_targets': len(pairs), 'unmatched_link_targets': 0}
    manifest['companies'][co] = slug
    manifest['stats'][co] = dict(summary, slug=slug, bfs=shard['bfs'], unmatched=0, onesided=0)
    evidence = {'company': co, 'verified_date': date, 'method': 'cloud_browser_dom',
                'scope': '現有正式網站現售商品及其附加保險連結中的主附約版本；非全部歷史商品',
                'summary': summary, 'pages': list(pages.values()),
                'pairs': [{'main': m, 'rider': r, 'source_codes': s} for (m, r), s in sorted(pairs.items())],
                'excluded': excluded, 'new_product_ids': added, 'status_corrections': corrections}
    dest = ROOT / 'data/riders'
    (dest / 'evidence').mkdir(exist_ok=True)
    (dest / 'evidence' / (slug+'.json')).write_text(dumps(evidence)+'\n')
    (dest / (slug+'.json')).write_text(dumps(shard)+'\n')
    (dest / (slug+'.js')).write_text('window.INSURANCE_PRODUCT_RIDERS_SHARD='+dumps(shard)+';\n')
    (dest / 'manifest.json').write_text(dumps(manifest)+'\n')
    (dest / 'manifest.js').write_text('window.INSURANCE_PRODUCT_RIDERS_MANIFEST='+dumps(manifest)+';\n')
    if added or corrections:
        meta['count'] = len(products)
        for stat, prop in [('insurers','insurer'),('sectors','sector'),('statuses','status'),('sources','source'),('categories','category')]:
            meta['stats'][stat] = dict(collections.Counter(p.get(prop, '未標示') for p in products))
        meta.setdefault('rider_checks', {})[co] = summary
        (ROOT / 'data/meta.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2)+'\n')
        updates = {'products': [p for p in products if p != original_products.get(p['id'])], 'meta': meta}
        overlay_path.write_text(dumps(updates)+'\n')
        (dest / 'product_updates.js').write_text('window.INSURANCE_RIDER_PRODUCT_UPDATES='+dumps(updates)+';\n')
    # Check coverage, bidirectional parity and exact source provenance.
    assert sum(len(e['r']) for e in shard['mt'].values()) == len(pairs)
    assert sum(len(e['m']) for e in shard['rt'].values()) == len(pairs)
    assert all(m in pages and r in pages for m, r in pairs)
    refresh_data_release()
    print(dumps(summary))


if __name__ == '__main__':
    build(*sys.argv[1:])
