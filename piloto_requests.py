"""Descarga HTTP por bloques; navegador solo para reutilizar sesion del piloto."""
import argparse
import json
import sqlite3
import time
from urllib.parse import urlsplit
import requests
from piloto import BASE, destination, sha256, validate

HOST = 'usilpe-my.sharepoint.com'
DOWNLOAD = ('https://usilpe-my.sharepoint.com/personal/sistemasinformacion_usil_pe/'
            '_layouts/15/download.aspx?UniqueId=fc6d3d14-10ab-4cfb-be1f-5ea8ecff0ce2&Translate=false')


def get_same_host(session, url):
    for _ in range(8):
        if urlsplit(url).scheme != 'https' or urlsplit(url).hostname != HOST:
            raise RuntimeError('AUTH_REQUIRED')
        response = session.get(url, stream=True, allow_redirects=False, timeout=(15, 45))
        if response.is_redirect:
            from urllib.parse import urljoin
            url = urljoin(url, response.headers['Location'])
            response.close()
            continue
        return response
    raise RuntimeError('TOO_MANY_REDIRECTS')


def run(args):
    config = json.loads((BASE / 'biblioteca.config.json').read_text('utf-8'))
    resource = json.loads((BASE / 'piloto.json').read_text('utf-8'))
    root, target = destination(config, resource)
    target.parent.mkdir(parents=True, exist_ok=True)
    report = root / 'validacion-requests.json'
    with sqlite3.connect(root / 'catalogo.sqlite3') as db:
        db.execute('CREATE TABLE IF NOT EXISTS resources (id TEXT PRIMARY KEY, path TEXT, sha256 TEXT, metadata TEXT)')
        row = db.execute('SELECT path,sha256 FROM resources WHERE id=?', (resource['resource_id'],)).fetchone()
        if target.exists():
            if row and row[0] == str(target) and sha256(target) == row[1]:
                print(json.dumps({'status': 'omitido_existente_verificado', 'path': str(target)}))
                return
            raise RuntimeError('UNREGISTERED_EXISTING_FILE')
    with requests.Session() as session:
        session.headers['User-Agent'] = 'Mozilla/5.0'
        if not args.anonymous:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                context = p.chromium.launch_persistent_context(
                    str(BASE / '.browser-profile'), channel='chrome',
                    headless=not args.login, accept_downloads=False)
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto(resource['url'], wait_until='domcontentloaded', timeout=60000)
                    if args.login:
                        print('Completa el inicio de sesion en Chrome. Esperando visor...', flush=True)
                        page.locator('#downloadCommand').wait_for(state='visible', timeout=300000)
                    cookies = context.cookies('https://' + HOST)
                    for c in cookies:
                        session.cookies.set(c['name'], c['value'], domain=c['domain'],
                                            path=c['path'], secure=c['secure'])
                    print(json.dumps({'profile_cookies_available': bool(cookies)}), flush=True)
                finally:
                    context.close()
        # Visitar el enlace compartido puede establecer las cookies propias del enlace.
        try:
            with get_same_host(session, resource['url']) as landing:
                print(json.dumps({'share_http': landing.status_code}), flush=True)
        except RuntimeError:
            pass
        with get_same_host(session, DOWNLOAD) as response:
            print(json.dumps({'download_http': response.status_code,
                              'content_type': response.headers.get('Content-Type')}), flush=True)
            if response.status_code != 200:
                report.write_text(json.dumps({'status': 'http_error', 'http': response.status_code}), 'utf-8')
                raise RuntimeError('HTTP_REJECTED')
            chunks = response.iter_content(1024 * 1024)
            first = next(chunks, b'')
            if not first.startswith(b'%PDF-'):
                report.write_text(json.dumps({'status': 'not_pdf'}), 'utf-8')
                raise RuntimeError('NOT_PDF_OR_LOGIN_REQUIRED')
            part = target.with_suffix('.pdf.part')
            with part.open('xb') as f:
                f.write(first)
                for chunk in chunks:
                    f.write(chunk)
            result = validate(part, resource['expected_pages'])
            if args.reference:
                from pathlib import Path
                result['matches_reference'] = sha256(Path(args.reference)) == result['sha256']
                if not result['matches_reference']:
                    raise RuntimeError('REFERENCE_MISMATCH')
            part.rename(target)
            result.update(status='downloaded_requests', path=str(target),
                          temporary_path=str(part), downloaded_at=time.time())
            with sqlite3.connect(root / 'catalogo.sqlite3') as db:
                db.execute('INSERT OR REPLACE INTO resources VALUES (?,?,?,?)',
                           (resource['resource_id'], str(target), result['sha256'], json.dumps(result)))
            report.write_text(json.dumps(result, indent=2), 'utf-8')
            print(json.dumps(result, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--anonymous', action='store_true')
    parser.add_argument('--login', action='store_true')
    parser.add_argument('--reference')
    args = parser.parse_args()
    try:
        run(args)
    except RuntimeError as e:
        print(str(e) if str(e).isupper() else 'PILOT_FAILED', flush=True)
        raise SystemExit(1)
    except Exception as e:
        print(type(e).__name__ + ': prueba incompleta', flush=True)
        raise SystemExit(1)
