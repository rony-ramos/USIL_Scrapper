"""Piloto Windows/Chrome: descarga CDP directamente a la carpeta final."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlparse

BASE = Path(__file__).resolve().parent


def component(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value)).strip(' .')
    if not value or value.upper().split('.')[0] in {
        'CON', 'PRN', 'AUX', 'NUL',
        *(f'COM{i}' for i in range(1, 10)),
        *(f'LPT{i}' for i in range(1, 10)),
    }:
        raise ValueError('Nombre de carpeta o archivo no valido')
    return value[:120].rstrip(' .')


def destination(config, resource):
    root = Path(config['root']).resolve()
    parts = [component(resource[k]) for k in
             ('periodo', 'curso', 'unidad', 'semana', 'archivo')]
    target = root.joinpath(*parts).resolve()
    if not target.is_relative_to(root):
        raise ValueError('Destino fuera de la biblioteca')
    return root, target


def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def validate(path, expected_pages):
    from pypdf import PdfReader
    with path.open('rb') as f:
        if f.read(5) != b'%PDF-':
            raise ValueError('La respuesta no es un PDF')
    reader = PdfReader(path)
    if reader.is_encrypted:
        raise ValueError('PDF cifrado')
    pages = len(reader.pages)
    if pages != expected_pages:
        raise ValueError(f'Paginas inesperadas: {pages}, esperadas {expected_pages}')
    text_pages = sum(bool((p.extract_text() or '').strip()) for p in reader.pages)
    return {'pages': pages, 'text_pages': text_pages,
            'bytes': path.stat().st_size, 'sha256': sha256(path)}


def run(args):
    config = json.loads((BASE / 'biblioteca.config.json').read_text('utf-8'))
    resource = json.loads((BASE / 'piloto.json').read_text('utf-8'))
    root, target = destination(config, resource)
    if args.plan:
        print(json.dumps({'destino': str(target), 'temporal_en': str(target.parent)}, indent=2))
        return
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / 'catalogo.sqlite3')
    db.execute('CREATE TABLE IF NOT EXISTS resources (id TEXT PRIMARY KEY, path TEXT, sha256 TEXT, metadata TEXT)')
    row = db.execute('SELECT path,sha256 FROM resources WHERE id=?', (resource['resource_id'],)).fetchone()
    if row and Path(row[0]) == target and target.exists() and sha256(target) == row[1]:
        print(json.dumps({'status': 'omitido_existente_verificado', 'path': str(target)}), flush=True)
        db.close()
        return
    if target.exists():
        raise RuntimeError('Hay un archivo no verificado en el destino; no se sobrescribe')
    target.parent.mkdir(parents=True, exist_ok=True)
    if urlparse(resource['url']).hostname != 'usilpe-my.sharepoint.com':
        raise ValueError('Host no autorizado para este piloto')

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(BASE / '.browser-profile'), channel='chrome', headless=False,
            accept_downloads=True, downloads_path=str(target.parent))
        try:
            page = context.pages[0] if context.pages else context.new_page()
            cdp = context.new_cdp_session(page)
            # Chrome escribe GUID.crdownload y luego GUID en la carpeta final.
            # No usamos save_as(), ni copiamos desde Descargas.
            cdp.send('Browser.setDownloadBehavior', {
                'behavior': 'allowAndName', 'downloadPath': str(target.parent),
                'eventsEnabled': True})
            state = {}

            def begun(event):
                state['guid'] = event['guid']

            def progress(event):
                if event['guid'] == state.get('guid'):
                    state['state'] = event['state']

            cdp.on('Browser.downloadWillBegin', begun)
            cdp.on('Browser.downloadProgress', progress)
            page.goto(resource['url'], wait_until='domcontentloaded', timeout=60000)
            print('Si Microsoft lo solicita, inicia sesion en la ventana de Chrome del piloto.', flush=True)
            button = page.locator('#downloadCommand')
            button.wait_for(state='visible', timeout=args.login_timeout * 1000)
            button.click()
            deadline = time.monotonic() + 180
            while state.get('state') not in ('completed', 'canceled'):
                if time.monotonic() > deadline:
                    raise TimeoutError('Descarga no completada en 180 segundos')
                page.wait_for_timeout(250)
            if state['state'] != 'completed':
                raise RuntimeError('Chrome cancelo la descarga')
            guid = state['guid']
            if not re.fullmatch(r'[a-zA-Z0-9-]+', guid):
                raise ValueError('Identificador de descarga no valido')
            downloaded = target.parent / guid
            result = validate(downloaded, resource['expected_pages'])
            if args.reference:
                result['matches_reference'] = result['sha256'] == sha256(Path(args.reference))
                if not result['matches_reference']:
                    raise ValueError('El PDF difiere de la referencia; se conserva sin publicar')
            # Renombrado en el mismo directorio y volumen; no hay traslado previo.
            downloaded.rename(target)
            result.update(status='descargado_validado', path=str(target),
                          temporary_directory=str(target.parent), source=resource['url'])
            db.execute('INSERT OR REPLACE INTO resources VALUES (?,?,?,?)',
                       (resource['resource_id'], str(target), result['sha256'], json.dumps(result)))
            db.commit()
            (root / 'validacion-piloto.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
            print(json.dumps(result, indent=2), flush=True)
        finally:
            context.close()
            db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--plan', action='store_true')
    parser.add_argument('--reference')
    parser.add_argument('--login-timeout', type=int, default=300)
    try:
        run(parser.parse_args())
    except Exception as exc:
        # Evita volcar URLs de autenticacion, cookies o trazas con datos de sesion.
        print(f'ERROR [{type(exc).__name__}]: piloto incompleto. No se registra como validado.', flush=True)
        raise SystemExit(1)
