"""SeleniumBase autentica; requests consulta Canvas y descarga materiales."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit, urljoin, quote

import requests
from piloto import BASE, component, sha256

CANVAS = 'https://usil.instructure.com'
SP = 'usilpe-my.sharepoint.com'


class AccessError(RuntimeError):
    pass


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
    def __init__(self, session):
        self.session = session

    def get(self, url):
        url = urljoin(CANVAS, url)
        if urlsplit(url).netloc != urlsplit(CANVAS).netloc or not url.startswith(CANVAS + '/api/v1/'):
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


def stream_file(session, url, target):
    """Redirecciones HTTPS sin reenviar cookies de Canvas a otros dominios."""
    for _ in range(8):
        host = urlsplit(url).hostname or ''
        allowed = (host in ('usil.instructure.com', SP) or host.endswith('.instructure.com')
                   or host.endswith('.instructureusercontent.com')
                   or host.endswith('.amazonaws.com'))
        if not allowed or urlsplit(url).scheme != 'https':
            raise AccessError('Destino requiere otro conector o inicio de sesion')
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


def sync(driver, api, root, selected, download):
    courses = api.all('/api/v1/courses?per_page=100')
    public_fields = ('id', 'name', 'course_code', 'enrollment_term_id', 'access_restricted_by_date')
    write_json(root / 'cursos.json', [{k: c[k] for k in public_fields if k in c} for c in courses])
    print(f'Inventario: {len(courses)} cursos. Seleccion: {len(selected)} IDs.', flush=True)
    results = []
    with sqlite3.connect(root / 'sync.sqlite3') as db:
        db.execute('CREATE TABLE IF NOT EXISTS runs (time TEXT, report TEXT)')
        for course in courses:
            cid = course['id']
            if cid not in selected:
                continue
            if not course.get('name'):
                results.append({'course': cid, 'status': 'acceso_restringido'})
                continue
            folder = course_folder(root, course)
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
                                path = directory / (str(item['id']) + '-' + component(detail['filename']))
                                record['status'], record['sha256'] = stream_file(api.session, detail['url'], path)
                                record['path'] = str(path.relative_to(root))
                            elif typ == 'ExternalUrl':
                                host = urlsplit(item['external_url']).hostname or ''
                                record['status'] = 'pendiente_conector' if '.' in host else 'enlace_incompleto'
                                if host == SP and download:
                                    driver.get(item['external_url'])
                                    # Endpoint realmente cargado por el visor; no adivina IDs.
                                    from selenium.webdriver.support.ui import WebDriverWait
                                    endpoint = WebDriverWait(driver, 30).until(lambda d: d.execute_script(
                                        "return performance.getEntriesByType('resource').map(e=>e.name).find(u=>u.includes('/_layouts/15/download.aspx')) || null"))
                                    if urlsplit(endpoint).hostname != SP:
                                        raise AccessError('Endpoint de SharePoint inesperado')
                                    with browser_session(driver, SP) as sp_session:
                                        # Piloto de enlaces PDF (:b:); otros tipos quedan pendientes.
                                        if '/:b:/' not in item['external_url']:
                                            raise AccessError('Tipo de SharePoint aun no implementado')
                                        path = directory / (str(item['id']) + '-' + component(item['title']) + '.pdf')
                                        record['status'], record['sha256'] = stream_file(sp_session, endpoint, path)
                                        record['path'] = str(path.relative_to(root))
                            else:
                                record['status'] = 'inventariado'
                        except Exception as exc:
                            record['status'] = 'pendiente'
                            record['error_type'] = type(exc).__name__
                        records.append(record)
                write_json(folder / 'recursos.json', records)
                results.append({'course': cid, 'resources': len(records), 'status': 'inventariado'})
                print(f'Curso {cid}: {len(records)} recursos inventariados.', flush=True)
            except Exception as exc:
                results.append({'course': cid, 'status': 'pendiente', 'error_type': type(exc).__name__})
        write_json(root / 'ultima-sincronizacion.json', results)
        db.execute('INSERT INTO runs VALUES (?,?)', (datetime.now(timezone.utc).isoformat(), json.dumps(results)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--download', action='store_true')
    parser.add_argument('--course', type=int, action='append')
    parser.add_argument('--login-timeout', type=int, default=600)
    args = parser.parse_args()
    from seleniumbase import Driver
    config = json.loads((BASE / 'biblioteca.config.json').read_text('utf-8'))
    root = Path(config['root']).resolve()
    root.mkdir(parents=True, exist_ok=True)
    selected = set(args.course or json.loads((BASE / 'cursos_seleccionados.json').read_text('utf-8'))['course_ids'])
    driver = Driver(browser='chrome', user_data_dir=str(BASE / '.browser-profile'),
                    headless=False)
    session = None
    try:
        driver.get(CANVAS + '/courses')
        print('Inicia sesion USIL en esta ventana. Python continuara cuando la API responda.', flush=True)
        deadline = time.monotonic() + args.login_timeout
        while time.monotonic() < deadline:
            if urlsplit(driver.current_url).hostname == 'usil.instructure.com':
                if session:
                    session.close()
                session = browser_session(driver, 'usil.instructure.com')
                api = CanvasAPI(session)
                try:
                    api.get('/api/v1/courses?per_page=1')
                    print('Canvas autenticado mediante requests.', flush=True)
                    sync(driver, api, root, selected, args.download)
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
        print('Proceso incompleto: ' + type(exc).__name__, flush=True)
        raise SystemExit(1)
