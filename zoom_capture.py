"""Adaptador web de Zoom LTI: captura solo solicitudes observadas de la sesion.

No es la API publica de Zoom. Si cambia la interfaz, deja pendientes y permite
importar VTT manualmente; no reconstruye URLs, tokens ni peticiones cURL.
"""
import base64
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import time
from urllib.parse import parse_qs, urljoin, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from canvas_sync import CanvasAPI, active_courses, browser_session, write_json
from knowledge_core import load_json, recording_identity, save_recording, transcript_verified


LIST_PATH = '/api/v1/lti/rich/recording/COURSE'
FILE_PATH = '/api/v1/lti/rich/recording/file'
ROW_SELECTOR = 'tr.ant-table-row.ant-table-row-level-0'


class ZoomPending(RuntimeError):
    """Razon corta que nunca incluye una URL autenticada."""


def zoom_host(host):
    return bool(host and (host == 'zoom.us' or host.endswith('.zoom.us')))


def transcript_request(url):
    parts = urlsplit(url)
    return (parts.scheme == 'https' and zoom_host(parts.hostname)
            and parts.path == '/rec/play/vtt'
            and parse_qs(parts.query).get('type') == ['transcript'])


def decode_body(response):
    return (base64.b64decode(response['body']).decode('utf-8')
            if response.get('base64Encoded') else response['body'])


def recording_from_row(cid, row):
    # Verificado contra listStartTime de la plataforma: startTime es UTC aunque
    # timezone indique la zona usada para representar la tabla de Zoom.
    instant = datetime.fromisoformat(row['startTime'].replace('Z', '+00:00'))
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    started = instant.astimezone(timezone.utc).isoformat()
    meeting = str(row['meetingNumber'])
    return {'class_id': recording_identity(cid, meeting, started, row.get('meetingId')),
            'meeting_id': meeting, 'started_at_utc': started,
            'occurrence_id': row.get('meetingId'),  # Solo en memoria; public_record lo excluye.
            'source_timezone': row.get('timezone'), 'topic': row.get('topic'),
            'duration_minutes': row.get('duration'), 'ui_start': row.get('listStartTime')}


def viewer_reference(url):
    parts = urlsplit(url)
    if (parts.scheme != 'https' or not zoom_host(parts.hostname)
            or not parts.path.startswith('/rec/play/') or parts.path == '/rec/play/vtt'):
        raise ZoomPending('visor_zoom_no_valido')
    return {'method': 'observed_viewer_path_sha256',
            'sha256': hashlib.sha256(parts.path.encode()).hexdigest()}


def recording_file_metadata(payload, record=None):
    """Normaliza la respuesta file; fechas locales y enlaces nunca se adivinan."""
    if not isinstance(payload, dict) or not isinstance(payload.get('recordingFiles'), list):
        raise ZoomPending('metadatos_grabacion_no_validos')
    occurrence = payload.get('meetingId')
    if not isinstance(occurrence, str) or not occurrence:
        raise ZoomPending('ocurrencia_zoom_no_identificada')
    if record and (occurrence != record.get('occurrence_id')
                   or str(payload.get('meetingNumber')) != record['meeting_id']):
        raise ZoomPending('metadatos_de_otra_grabacion')
    zone_name = payload.get('timezone')
    if isinstance(zone_name, str):
        zone_name = re.sub(r'\s+', ' ', zone_name).strip()
    # Etiqueta localizada observada en la respuesta real del usuario. No aplicar
    # el offset actual fijo: ZoneInfo calcula tambien los cambios de horario.
    if zone_name == 'Hora del pacífico (EE. UU. y Canadá)':
        zone_name = 'America/Los_Angeles'
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise ZoomPending('zona_horaria_zoom_no_verificada') from None
    files = []
    for item in payload['recordingFiles']:
        if not isinstance(item, dict) or item.get('fileType') not in ('MP4', 'M4A'):
            continue
        try:
            def instant(value):
                parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                if parsed.tzinfo is None:
                    if (parsed.replace(tzinfo=zone, fold=0).utcoffset()
                            != parsed.replace(tzinfo=zone, fold=1).utcoffset()):
                        raise ZoomPending('fecha_zoom_ambigua_sin_offset')
                    parsed = parsed.replace(tzinfo=zone)
                return parsed.astimezone(timezone.utc)
            start, end = instant(item['recordingStart']), instant(item['recordingEnd'])
            if end < start or not isinstance(item['id'], str) or not item['id']:
                raise ValueError()
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ZoomPending('fechas_archivo_zoom_no_validas') from None
        entry = {'file_id_sha256': hashlib.sha256(item['id'].encode()).hexdigest(),
                 'file_type': item['fileType'], 'started_at_utc': start.isoformat(),
                 'ended_at_utc': end.isoformat()}
        if item.get('playUrl'):
            entry['recording_reference'] = viewer_reference(item['playUrl'])
        files.append(entry)
    if not files:
        raise ZoomPending('metadatos_sin_archivos_de_grabacion')
    return {'acquisition': 'zoom_observed_file_response', 'source_timezone': zone.key,
            'occurrence_sha256': hashlib.sha256(occurrence.encode()).hexdigest(), 'files': files}


def capture_detail(driver, trigger):
    """Una respuesta del endpoint file verificado; sin capturar peticiones ni cookies."""
    driver.execute_script(r"""
      if(location.hostname !== 'applications.zoom.us') throw new Error('unexpected_origin');
      const target = arguments[0];
      const open = XMLHttpRequest.prototype.open, send = XMLHttpRequest.prototype.send;
      window.__knowledgeDetailCapture = {pending:true};
      let timer;
      const restore = () => {
        XMLHttpRequest.prototype.open = open; XMLHttpRequest.prototype.send = send;
        clearTimeout(timer); window.__knowledgeDetailRestore = null;
      };
      window.__knowledgeDetailRestore = restore;
      XMLHttpRequest.prototype.open = function(method,url,...rest) {
        const u = new URL(url,location.href);
        this.__knowledgeDetail = u.origin === location.origin && u.pathname === target;
        return open.call(this,method,url,...rest);
      };
      XMLHttpRequest.prototype.send = function(...args) {
        if(this.__knowledgeDetail) this.addEventListener('loadend',() => {
          let payload = null;
          try {
            const data = this.responseType === 'json' ? this.response : JSON.parse(this.responseText);
            const r = data.result;
            if(data.status === true && r && Array.isArray(r.recordingFiles)) payload = {
              meetingId:r.meetingId, meetingNumber:r.meetingNumber, timezone:r.timezone,
              recordingFiles:r.recordingFiles.map(f => ({id:f.id, fileType:f.fileType,
                recordingStart:f.recordingStart, recordingEnd:f.recordingEnd, playUrl:f.playUrl}))};
          } catch(_) {}
          window.__knowledgeDetailCapture = {pending:false,status:this.status,payload};
          restore();
        },{once:true});
        return send.apply(this,args);
      };
      timer = setTimeout(restore,22000);
    """, FILE_PATH)
    try:
        trigger()
        captured = wait_for(lambda: driver.execute_script(
            'return window.__knowledgeDetailCapture?.pending === false ? window.__knowledgeDetailCapture : null'), 24)
        if captured['status'] in (401, 403):
            raise ZoomPending('sesion_zoom_requiere_revision')
        if captured['status'] != 200 or not captured.get('payload'):
            raise ZoomPending('metadatos_grabacion_no_validos')
        return captured['payload']
    finally:
        driver.execute_script('if(window.__knowledgeDetailRestore) window.__knowledgeDetailRestore(); delete window.__knowledgeDetailCapture;')


def wait_for(callback, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = callback()
        if value:
            return value
        time.sleep(0.4)
    raise ZoomPending('timeout_interfaz')


def zoom_frame(driver):
    driver.switch_to.default_content()
    frames = driver.find_elements('css selector', 'iframe[title="Zoom"]')
    if len(frames) != 1:
        raise ZoomPending('iframe_zoom_no_disponible')
    driver.switch_to.frame(frames[0])


def click_text(driver, selector, labels):
    wanted = {s.casefold() for s in labels}
    elements = [e for e in driver.find_elements('css selector', selector)
                if e.is_displayed() and e.text.strip().casefold() in wanted]
    if len(elements) != 1:
        raise ZoomPending('control_no_identificado')
    elements[0].click()


def install_list_capture(driver):
    """Hook de un solo XHR, limitado al endpoint ya observado; sin cabeceras."""
    driver.execute_script(r"""
      const target = arguments[0];
      const observed = performance.getEntriesByType('resource').some(e => {
        const u = new URL(e.name); return u.origin === location.origin && u.pathname === target;
      });
      if (!observed || location.hostname !== 'applications.zoom.us')
        throw new Error('list_request_not_observed');
      if (window.__knowledgeListRestore) window.__knowledgeListRestore();
      const open = XMLHttpRequest.prototype.open, send = XMLHttpRequest.prototype.send;
      window.__knowledgeListCapture = {pending:true};
      let timer;
      const restore = () => {
        XMLHttpRequest.prototype.open = open; XMLHttpRequest.prototype.send = send;
        clearTimeout(timer); window.__knowledgeListRestore = null;
      };
      window.__knowledgeListRestore = restore;
      XMLHttpRequest.prototype.open = function(method, url, ...rest) {
        const u = new URL(url, location.href);
        this.__knowledgeList = u.origin === location.origin && u.pathname === target;
        return open.call(this, method, url, ...rest);
      };
      XMLHttpRequest.prototype.send = function(...args) {
        if (this.__knowledgeList) this.addEventListener('loadend', () => {
          let payload = null;
          try {
            const data = this.responseType === 'json' ? this.response : JSON.parse(this.responseText);
            const r = data.result;
            if (r && Array.isArray(r.list)) payload = {
              pageNum:r.pageNum, pageSize:r.pageSize, total:r.total,
              list:r.list.map(x => ({meetingId:x.meetingId, meetingNumber:x.meetingNumber,
                startTime:x.startTime, listStartTime:x.listStartTime,
                timezone:x.timezone, topic:x.topic, duration:x.duration}))};
          } catch (_) {}
          window.__knowledgeListCapture = {pending:false,status:this.status,payload};
          restore();
        }, {once:true});
        return send.apply(this,args);
      };
      timer = setTimeout(restore,20000);
    """, LIST_PATH)


def capture_list(driver, trigger):
    install_list_capture(driver)
    try:
        trigger()
        captured = wait_for(lambda: driver.execute_script(
            'return window.__knowledgeListCapture?.pending === false ? window.__knowledgeListCapture : null'), 22)
        if captured['status'] in (401, 403):
            raise ZoomPending('sesion_zoom_requiere_revision')
        if captured['status'] != 200 or not captured.get('payload'):
            raise ZoomPending('listado_zoom_no_valido')
        page = captured['payload']
        if not isinstance(page['pageSize'], int) or page['pageSize'] < 1 or not isinstance(page['total'], int):
            raise ZoomPending('paginacion_zoom_no_valida')
        wait_for(lambda: len(driver.find_elements('css selector', ROW_SELECTOR)) == len(page['list']) or not page['list'])
        return page
    finally:
        driver.execute_script('if(window.__knowledgeListRestore) window.__knowledgeListRestore(); delete window.__knowledgeListCapture;')


def discover(driver, cid):
    wait_for(lambda: driver.find_elements('css selector', '[role="tab"]'))
    click_text(driver, '[role="tab"]', ('Grabaciones en la nube', 'Cloud Recordings'))
    wait_for(lambda: driver.execute_script('return performance.getEntriesByType("resource").some(e=>new URL(e.name).pathname===arguments[0])', LIST_PATH))
    page = capture_list(driver, lambda: click_text(driver, 'button', ('Buscar', 'Search')))
    rows, page_count = [], math.ceil(page['total'] / page['pageSize'])
    # Recorrer paginas observadas, verificando pagina y total para no truncar el lote.
    for expected in range(1, max(1, page_count)+1):
        if page['pageNum'] != expected:
            raise ZoomPending('pagina_zoom_inesperada')
        rows.extend({**recording_from_row(cid, r), 'page': expected} for r in page['list'])
        if expected < page_count:
            controls = driver.find_elements('css selector', '.ant-pagination-next:not(.ant-pagination-disabled)')
            if len(controls) != 1:
                raise ZoomPending('paginacion_zoom_no_disponible')
            page = capture_list(driver, controls[0].click)
    if len(rows) != page['total'] or len({r['class_id'] for r in rows}) != len(rows):
        raise ZoomPending('listado_zoom_incompleto_o_duplicado')
    return rows


def select_recording(driver, record):
    # Volver al listado si se viene de otra grabacion.
    if not driver.find_elements('css selector', ROW_SELECTOR):
        click_text(driver, '[role="button"]', ('Grabaciones durante el curso', 'Course Recordings'))
        wait_for(lambda: driver.find_elements('css selector', ROW_SELECTOR))
    current = driver.find_elements('css selector', '.ant-pagination-item-active')
    if current and int(current[0].text) != record['page']:
        buttons = driver.find_elements('css selector', f'.ant-pagination-item-{record["page"]}')
        if len(buttons) != 1:
            raise ZoomPending('pagina_grabacion_no_disponible')
        capture_list(driver, buttons[0].click)
    normalize = lambda s: re.sub(r'\s+', ' ', s).strip()
    matching = []
    for row in driver.find_elements('css selector', ROW_SELECTOR):
        cells = row.find_elements('css selector', 'td')
        if len(cells) == 3 and re.sub(r'\s+', '', cells[1].text) == record['meeting_id'] and normalize(cells[2].text) == normalize(record['ui_start']):
            matching.append(cells[0])
    if len(matching) != 1:
        raise ZoomPending('fila_grabacion_no_identificada')
    detail = capture_detail(driver, lambda: matching[0].find_element('css selector', '[role="button"]').click())
    wait_for(lambda: any(e.is_displayed() and e.text.strip().casefold() in
                        {'grabaciones durante el curso', 'course recordings'}
                        for e in driver.find_elements('css selector', '[role="button"]')))
    return recording_file_metadata(detail, record)


def public_record(record):
    keys = ('class_id', 'meeting_id', 'started_at_utc', 'source_timezone', 'topic', 'duration_minutes', 'zoom_detail')
    return {k: record[k] for k in keys if k in record}


def sync_zoom(config, context, dry_run=False, refresh=False, limit=None, login_timeout=120):
    from seleniumbase import Driver
    driver, session = None, None
    report = {'discovered': 0, 'metadata_classes': 0, 'verified_classes': 0, 'pending': 0, 'deferred': 0}
    inventory_path = context['root'] / 'grabaciones.json'
    old = {r['class_id']: r for r in load_json(inventory_path, [])}
    inventory = []
    try:
        driver = Driver(browser='chrome', user_data_dir=config['browser_profile'],
                        headless=False, page_load_strategy='eager')
        driver.set_page_load_timeout(40)
        driver.get(config['canvas_url'] + '/courses')
        print('Verificando sesion Canvas; completa el inicio de sesion si Chrome lo solicita.', flush=True)
        deadline = time.monotonic() + login_timeout
        cid = context['course']['id']
        while time.monotonic() < deadline:
            if urlsplit(driver.current_url).hostname == urlsplit(config['canvas_url']).hostname:
                if session:
                    session.close()
                session = browser_session(driver, urlsplit(config['canvas_url']).hostname)
                api = CanvasAPI(session, config['canvas_url'])
                try:
                    active = active_courses(api, config.get('current_period'))
                    if cid not in {r['id'] for r in active}:
                        raise ValueError('El curso no figura como activo en el periodo configurado')
                    break
                except ValueError:
                    raise
                except Exception:
                    pass
            time.sleep(2)
        else:
            raise ZoomPending('inicio_sesion_canvas_pendiente')
        tabs = api.all(f'/api/v1/courses/{cid}/tabs')
        zoom = [t for t in tabs if t.get('label', '').casefold() == 'zoom']
        if len(zoom) != 1 or not zoom[0].get('html_url'):
            raise ZoomPending('curso_sin_pestana_zoom')
        url = urljoin(config['canvas_url'] + '/', zoom[0]['html_url'])
        if urlsplit(url).netloc != urlsplit(config['canvas_url']).netloc:
            raise ZoomPending('pestana_fuera_canvas')
        driver.get(url)
        wait_for(lambda: driver.find_elements('css selector', 'iframe[title="Zoom"]'), 40)
        zoom_frame(driver)
        rows = discover(driver, cid)
        report['discovered'] = len(rows)
        print(f'Zoom: {len(rows)} grabaciones disponibles.', flush=True)
        if dry_run:
            report['recordings'] = [public_record(r) for r in rows]
            return report
        attempted = 0
        for number, record in enumerate(rows, 1):
            entry = {**old.get(record['class_id'], {}), **public_record(record)}
            inventory.append(entry)
            save_recording(context, record)
            entry['transcript_status'] = 'imported' if transcript_verified(context, record['class_id']) else 'pending_import'
            if not refresh and entry.get('status') == 'metadata_captured' and entry.get('zoom_detail'):
                save_recording(context, {**record, 'zoom_detail': entry['zoom_detail']})
                report['verified_classes'] += 1
                continue
            if limit and attempted >= limit:
                entry['status'] = 'deferred'
                report['deferred'] += 1
                continue
            attempted += 1
            try:
                detail = select_recording(driver, record)
                record = {**record, 'zoom_detail': detail}
                entry['zoom_detail'] = detail
                save_recording(context, record)
                entry.update(status='metadata_captured')
                entry.pop('reason', None)
                report['metadata_classes'] += 1
                print(f'Zoom {number}/{len(rows)}: metadatos verificados; transcripcion {entry["transcript_status"]}.', flush=True)
            except Exception as exc:
                entry.update(status='pending', reason=str(exc) if isinstance(exc, ZoomPending) else type(exc).__name__)
                report['pending'] += 1
                print(f'Zoom {number}/{len(rows)}: pendiente ({entry["reason"]}).', flush=True)
            write_json(inventory_path, inventory + [old[r['class_id']] for r in rows[number:] if r['class_id'] in old])
        write_json(inventory_path, inventory)
        return report
    except ZoomPending as exc:
        report.update(pending=max(1, report['pending']), reason=str(exc))
        return report
    finally:
        if session:
            session.close()
        if driver:
            driver.quit()
