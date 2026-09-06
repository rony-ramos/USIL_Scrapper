"""SeleniumBase autentica; requests consulta Canvas y descarga materiales."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from contextlib import closing
import time
from urllib.parse import urlsplit, urljoin, quote

import requests
from piloto import BASE, component, sha256
from settings import load_settings


class AccessError(RuntimeError):
    pass


class SharePointUnavailable(AccessError):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


def resolve_sharepoint(driver, timeout=30):
    """Detecta error visible antes de esperar un endpoint que nunca llegara."""
    from selenium.webdriver.support.ui import WebDriverWait
    state = WebDriverWait(driver, timeout, poll_frequency=0.5).until(lambda d: d.execute_script(
        """const error = document.querySelector('#ms-error-body');
        if (error) return {error: true, message:
          (document.querySelector('#ctl00_PlaceHolderMain_LabelMessage')?.textContent ||
           document.querySelector('#ms-error-header')?.textContent || 'Error de SharePoint').trim().slice(0,500)};
        const url = performance.getEntriesByType('resource').map(e=>e.name)
          .find(u=>u.includes('/_layouts/15/download.aspx'));
        return url ? {url} : null;"""))
    if state.get('error'):
        raise SharePointUnavailable(state['message'])
    return state['url']


def browser_session(driver, host):
    """Solo cookies aplicables al host activo, sin serializarlas ni imprimirlas."""
    session = requests.Session()
    session.headers['User-Agent'] = driver.execute_script('return navigator.userAgent')
    for c in driver.get_cookies():
        domain = c.get('domain', '').lstrip('.')
        if domain and (host == domain or host.endswith('.' + domain)):
            session.cookies.set(c['name'], c['value'], domain=c['domain'],
                                path=c.get('path', '/'), secure=c.get('secure', True))
    return session


class CanvasAPI:
    def __init__(self, session, canvas_url):
        self.session = session
        self.base_url = canvas_url.rstrip('/')

    def get(self, url):
        url = urljoin(self.base_url, url)
        if urlsplit(url).netloc != urlsplit(self.base_url).netloc or not url.startswith(self.base_url + '/api/v1/'):
            raise AccessError('URL de API fuera de Canvas')
        for attempt in range(3):
            response = self.session.get(url, allow_redirects=False, timeout=(15, 45))
            if response.status_code == 429 or response.status_code >= 500:
                response.close()
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
            if response.status_code != 200:
                code = response.status_code
                response.close()
                raise AccessError(f'Canvas HTTP {code}')
            try:
                body = response.text
                if body.startswith('while(1);'):
                    body = body[len('while(1);'):]
                return json.loads(body), response.links.get('next', {}).get('url')
            except ValueError:
                raise AccessError('Canvas devolvio HTML o una sesion no valida') from None
            finally:
                response.close()
        raise AccessError('Canvas temporalmente no disponible')

    def all(self, url):
        seen, output = set(), []
        while url:
            if url in seen:
                raise AccessError('Paginacion ciclica')
            seen.add(url)
            rows, url = self.get(url)
            if not isinstance(rows, list):
                raise AccessError('Se esperaba una lista')
            output.extend(rows)
        return output


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(path.suffix + '.part')
    part.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(part, path)


def stream_file(session, url, target, allowed_hosts):
    """Redirecciones HTTPS sin reenviar cookies de Canvas a otros dominios."""
    for _ in range(8):
        host = urlsplit(url).hostname or ''
        allowed = host in allowed_hosts
        if not allowed or urlsplit(url).scheme != 'https':
            raise AccessError('Host de descarga no permitido: ' + host)
        response = session.get(url, stream=True, allow_redirects=False, timeout=(15, 60))
        if response.is_redirect:
            url = urljoin(url, response.headers['Location'])
            response.close()
            continue
        break
    else:
        raise AccessError('Demasiadas redirecciones')
    with response:
        if response.status_code != 200:
            raise AccessError(f'Descarga HTTP {response.status_code}')
        if 'text/html' in response.headers.get('Content-Type', '').lower():
            raise AccessError('La descarga devolvio una pagina web')
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_suffix(target.suffix + '.part')
        try:
            with part.open('wb') as f:
                for chunk in response.iter_content(1024 * 1024):
                    f.write(chunk)
            if not part.stat().st_size:
                raise AccessError('Archivo vacio')
            if target.suffix.lower() == '.pdf':
                from pypdf import PdfReader
                reader = PdfReader(part)
                if reader.is_encrypted or not len(reader.pages):
                    raise AccessError('PDF no legible')
            digest = sha256(part)
            if target.exists() and sha256(target) == digest:
                part.unlink()
                return 'sin_cambios', digest
            if target.exists():
                history = target.parent / 'versiones'
                history.mkdir(exist_ok=True)
                previous = history / (target.stem + '-' + sha256(target)[:12] + target.suffix)
                if previous.exists():
                    if sha256(previous) != sha256(target):
                        raise AccessError('Colision de version')
                    target.unlink()
                else:
                    target.rename(previous)
            os.replace(part, target)
            return 'descargado', digest
        except Exception:
            # Solo este temporal, calculado dentro del destino, se limpia.
            if part.exists():
                part.unlink()
            raise


def course_folder(root, course):
    name = course['name']
    period = re.search(r'20\d\d-\d\d', name)
    period = period.group() if period else 'Sin periodo'
    name = re.sub(r'^\d+\s*-\s*', '', name).split(' - 20')[0]
    return root / component(period) / (component(name) + f" [{course['id']}]")


def sync(driver, api, root, selected, download, config, courses=None, resume=False):
    courses = courses if courses is not None else api.all('/api/v1/courses?per_page=100')
    hosts = {urlsplit(api.base_url).hostname, *config['sharepoint_hosts'], *config['download_hosts']}
    public_fields = ('id', 'name', 'course_code', 'enrollment_term_id', 'access_restricted_by_date')
    write_json(root / 'cursos.json', [{k: c[k] for k in public_fields if k in c} for c in courses])
    print(f'Inventario: {len(courses)} cursos. Seleccion: {len(selected)} IDs.', flush=True)
    results = []
    with closing(sqlite3.connect(root / 'sync.sqlite3')) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS runs (time TEXT, report TEXT)')
        for course in courses:
            cid = course['id']
            if cid not in selected:
                continue
            if not course.get('name'):
                results.append({'course': cid, 'status': 'acceso_restringido'})
                continue
            folder = course_folder(root, course)
            prior = {}
            if resume and (folder / 'recursos.json').exists():
                prior = {r['id']: r for r in json.loads((folder / 'recursos.json').read_text('utf-8'))}
            try:
                modules = api.all(f'/api/v1/courses/{cid}/modules?per_page=100')
                records = []
                for mod in modules:
                    items = api.all(f"/api/v1/courses/{cid}/modules/{mod['id']}/items?per_page=100")
                    week = 'General'
                    for item in items:
                        if item['type'] == 'SubHeader':
                            week = component(item['title'])
                            continue
                        record = {k: item[k] for k in ('id', 'title', 'type', 'external_url', 'page_url', 'content_id') if k in item}
                        record.update(module_id=mod['id'], module=mod['name'], week=week)
                        directory = folder / component(mod['name']) / week
                        try:
                            old = prior.get(item['id'], {})
                            old_path = (root / old.get('path', '')).resolve()
                            if (resume and old.get('sha256') and old.get('external_url') == item.get('external_url')
                                    and old.get('content_id') == item.get('content_id')
                                    and old_path.is_relative_to(root.resolve()) and old_path.is_file()
                                    and sha256(old_path) == old['sha256']):
                                record.update(status='existente_verificado', path=old['path'], sha256=old['sha256'])
                                records.append(record)
                                continue
                            typ = item['type']
                            if typ == 'Page':
                                detail, _ = api.get(f"/api/v1/courses/{cid}/pages/{quote(item['page_url'], safe='')}")
                                write_json(directory / f"{item['id']}-pagina.json", {k: detail.get(k) for k in ('title', 'body', 'updated_at', 'url')})
                                record['status'] = 'contenido_guardado'
                            elif typ == 'Assignment':
                                detail, _ = api.get(f"/api/v1/courses/{cid}/assignments/{item['content_id']}")
                                write_json(directory / f"{item['id']}-tarea.json", {k: detail.get(k) for k in ('name', 'description', 'updated_at', 'due_at')})
                                record['status'] = 'contenido_guardado'
                            elif typ == 'File' and download:
                                detail, _ = api.get(f"/api/v1/files/{item['content_id']}")
                                # El endpoint proviene de la API autenticada de Canvas.
                                file_hosts = hosts | {urlsplit(detail['url']).hostname}
                                path = directory / (str(item['id']) + '-' + component(detail['filename']))
                                record['status'], record['sha256'] = stream_file(api.session, detail['url'], path, file_hosts)
                                record['path'] = str(path.relative_to(root))
                            elif typ == 'ExternalUrl':
                                host = urlsplit(item['external_url']).hostname or ''
                                record['status'] = 'pendiente_conector' if '.' in host else 'enlace_incompleto'
                                if host in config['sharepoint_hosts'] and download:
                                    if '/:b:/' not in item['external_url']:
                                        raise AccessError('Tipo de SharePoint aun no implementado')
                                    driver.get(item['external_url'])
                                    # Endpoint realmente cargado por el visor; no adivina IDs.
                                    endpoint = resolve_sharepoint(driver)
                                    if urlsplit(endpoint).hostname != host:
                                        raise AccessError('Endpoint de SharePoint inesperado')
                                    with browser_session(driver, host) as sp_session:
                                        # Piloto de enlaces PDF (:b:); otros tipos quedan pendientes.
                                        path = directory / (str(item['id']) + '-' + component(item['title']) + '.pdf')
                                        record['status'], record['sha256'] = stream_file(sp_session, endpoint, path, hosts)
                                        record['path'] = str(path.relative_to(root))
                            else:
                                record['status'] = 'inventariado'
                        except SharePointUnavailable as exc:
                            record['status'] = 'sin_acceso_sharepoint'
                            record['message'] = exc.message
                            record['checked_at'] = datetime.now(timezone.utc).isoformat()
                        except Exception as exc:
                            record['status'] = 'pendiente'
                            record['error_type'] = type(exc).__name__
                            if isinstance(exc, AccessError):
                                record['reason'] = str(exc)
                        records.append(record)
                    write_json(folder / 'recursos.json', records)
                    print(f"Curso {cid}, modulo {mod['id']}: {len(records)} recursos procesados.", flush=True)
                write_json(folder / 'recursos.json', records)
                from collections import Counter
                results.append({'course': cid, 'resources': len(records), 'status': 'inventariado',
                                'counts': dict(Counter(r['status'] for r in records))})
                print(f'Curso {cid}: {len(records)} recursos inventariados.', flush=True)
            except Exception as exc:
                results.append({'course': cid, 'status': 'pendiente', 'error_type': type(exc).__name__})
            write_json(root / 'ultima-sincronizacion.json', results)
        db.execute('INSERT INTO runs VALUES (?,?)', (datetime.now(timezone.utc).isoformat(), json.dumps(results)))


def retry_files(api, root, selected, config):
    """Reintenta solo archivos Canvas pendientes sin volver a abrir SharePoint."""
    hosts = {urlsplit(api.base_url).hostname, *config['sharepoint_hosts'], *config['download_hosts']}
    summary_path = root / 'ultima-sincronizacion.json'
    summary = json.loads(summary_path.read_text('utf-8')) if summary_path.exists() else []
    for course in api.all('/api/v1/courses?per_page=100'):
        if course['id'] not in selected or not course.get('name'):
            continue
        folder = course_folder(root, course)
        index = folder / 'recursos.json'
        if not index.exists():
            continue
        records = json.loads(index.read_text('utf-8'))
        for record in records:
            if record['type'] != 'File' or record['status'] not in ('pendiente', 'inventariado'):
                continue
            try:
                detail, _ = api.get(f"/api/v1/files/{record['content_id']}")
                record['download_host'] = urlsplit(detail['url']).hostname
                path = folder / component(record['module']) / component(record['week']) / (str(record['id']) + '-' + component(detail['filename']))
                record['status'], record['sha256'] = stream_file(api.session, detail['url'], path, hosts | {record['download_host']})
                record['path'] = str(path.relative_to(root))
                record.pop('error_type', None)
                record.pop('reason', None)
            except Exception as exc:
                record['status'] = 'pendiente'
                record['error_type'] = type(exc).__name__
                if isinstance(exc, AccessError):
                    record['reason'] = str(exc)
            print(f"Archivo {record['id']}: {record['status']}", flush=True)
        write_json(index, records)
        from collections import Counter
        counts = dict(Counter(r['status'] for r in records))
        for row in summary:
            if row['course'] == course['id']:
                row['counts'] = counts
    write_json(summary_path, summary)
    with closing(sqlite3.connect(root / 'sync.sqlite3')) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS runs (time TEXT, report TEXT)')
        db.execute('INSERT INTO runs VALUES (?,?)', (datetime.now(timezone.utc).isoformat(), json.dumps(summary)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--download', action='store_true')
    parser.add_argument('--course', type=int, action='append')
    parser.add_argument('--login-timeout', type=int, default=600)
    parser.add_argument('--config')
    parser.add_argument('--list-courses', action='store_true')
    parser.add_argument('--retry-files', action='store_true', help='Reintentar solo archivos Canvas pendientes del inventario local')
    parser.add_argument('--resume', action='store_true', help='Reusar archivos locales verificados de un lote interrumpido; no comprueba cambios remotos de esos archivos')
    args = parser.parse_args()
    from seleniumbase import Driver
    config = load_settings(args.config)
    root = Path(config['root']).resolve()
    root.mkdir(parents=True, exist_ok=True)
    selected = set(args.course or ([] if args.list_courses else json.loads(Path(config['courses_file']).read_text('utf-8-sig'))['course_ids']))
    if not args.list_courses and not selected:
        raise ValueError('Seleccion vacia: ejecuta --list-courses y configura course_ids o usa --course ID')
    driver = Driver(browser='chrome', user_data_dir=config['browser_profile'],
                    headless=False, page_load_strategy='eager')
    driver.set_page_load_timeout(40)
    session = None
    try:
        driver.get(config['canvas_url'] + '/courses')
        print('Inicia sesion en esta ventana. Python continuara cuando la API responda.', flush=True)
        deadline = time.monotonic() + args.login_timeout
        while time.monotonic() < deadline:
            if urlsplit(driver.current_url).hostname == urlsplit(config['canvas_url']).hostname:
                if session:
                    session.close()
                session = browser_session(driver, urlsplit(config['canvas_url']).hostname)
                api = CanvasAPI(session, config['canvas_url'])
                try:
                    api.get('/api/v1/courses?per_page=1')
                    print('Canvas autenticado mediante requests.', flush=True)
                    if args.list_courses:
                        rows = api.all('/api/v1/courses?per_page=100')
                        rows = [{k: r[k] for k in ('id', 'name', 'access_restricted_by_date') if k in r} for r in rows]
                        write_json(root / 'cursos.json', rows)
                        for row in rows:
                            print(f"{row['id']}: {row.get('name', 'Acceso restringido')}", flush=True)
                    elif args.retry_files:
                        retry_files(api, root, selected, config)
                    else:
                        sync(driver, api, root, selected, args.download, config, resume=args.resume)
                    return
                except AccessError:
                    pass
            time.sleep(2)
        raise AccessError('Inicio de sesion pendiente; vuelve a ejecutar cuando estes listo')
    finally:
        if session:
            session.close()
        driver.quit()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        if isinstance(exc, (ValueError, FileNotFoundError)):
            print(str(exc), flush=True)
        print('Proceso incompleto: ' + type(exc).__name__, flush=True)
        raise SystemExit(1)
