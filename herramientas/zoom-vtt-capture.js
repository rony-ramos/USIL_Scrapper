/* Captura de transcripciones Zoom desde el reproductor abierto en el navegador del usuario.
   Se ejecuta en la pagina /rec/play/ (marcador del navegador o agente con el navegador del usuario).
   - Solo reutiliza solicitudes VTT que el reproductor ya hizo; no construye URLs ni tokens.
   - Lee la respuesta preferentemente de la cache del navegador para no repetir la solicitud.
   - El navegador agrega sus propias cookies; este script no las lee.
   - Escribe zoom-vtt-<id>.json en la bandeja de capturas: VTT y dos hashes, sin URLs, cookies ni cabeceras.
   - La bandeja se elige una vez y debe contener el marcador .usil-zoom-capture-inbox creado por
     conocimiento.py --init-capture-inbox. Con "Permitir en cada visita" no vuelve a pedir permiso.
   - Devuelve solo un resumen (id, segmentos), nunca el texto ni la URL.
   Sin comentarios de linea para que funcione como marcador javascript:. */
(async function capture() {
  const MARKER = '.usil-zoom-capture-inbox';
  const toast = (text) => {
    try {
      const box = document.createElement('div');
      box.textContent = text;
      box.style.cssText = 'position:fixed;z-index:2147483647;right:16px;bottom:16px;max-width:360px;' +
        'padding:10px 14px;border-radius:8px;background:#1f2937;color:#fff;font:13px/1.4 sans-serif;';
      document.body.appendChild(box);
      setTimeout(() => box.remove(), 7000);
    } catch (e) { /* sin interfaz disponible */ }
  };
  const done = (result) => {
    toast(result.status === 'captured'
      ? 'Transcripcion guardada en la bandeja: ' + result.files.map((f) => f.id + ' (' + f.cues + ' segmentos)').join(', ')
      : 'Captura VTT pendiente: ' + result.reason);
    return result;
  };
  const zoomHost = (h) => h === 'zoom.us' || h.endsWith('.zoom.us');
  if (location.protocol !== 'https:' || !zoomHost(location.hostname) ||
      !location.pathname.startsWith('/rec/play/') || location.pathname === '/rec/play/vtt') {
    return done({status: 'pending', reason: 'abrir_desde_reproductor_zoom'});
  }
  if (!window.showDirectoryPicker || !window.indexedDB) {
    return done({status: 'pending', reason: 'navegador_sin_acceso_a_carpetas'});
  }
  const activated = () => Boolean(navigator.userActivation && navigator.userActivation.isActive);
  const db = () => new Promise((resolve, reject) => {
    const request = indexedDB.open('usil-scrapper-capture', 1);
    request.onupgradeneeded = () => request.result.createObjectStore('handles');
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  const stored = async (value) => {
    const conn = await db();
    try {
      return await new Promise((resolve, reject) => {
        const tx = conn.transaction('handles', value === undefined ? 'readonly' : 'readwrite');
        const store = tx.objectStore('handles');
        const request = value === undefined ? store.get('inbox') : store.put(value, 'inbox');
        request.onsuccess = () => resolve(value === undefined ? request.result : value);
        request.onerror = () => reject(request.error);
      });
    } finally { conn.close(); }
  };
  const hasMarker = async (dir) => {
    try { await dir.getFileHandle(MARKER); return true; } catch (e) { return false; }
  };
  let inbox = await stored().catch(() => null);
  let permission = inbox ? await inbox.queryPermission({mode: 'readwrite'}).catch(() => 'denied') : 'none';
  if (permission !== 'granted') {
    if (!activated()) {
      /* Sin gesto del usuario el navegador no permite elegir ni autorizar la carpeta:
         se ofrece un boton que el usuario pulsa una vez. */
      try {
        document.getElementById('usil-capture-setup')?.remove();
        const button = document.createElement('button');
        button.id = 'usil-capture-setup';
        button.textContent = inbox ? 'Autorizar bandeja de capturas' : 'Elegir bandeja de capturas';
        button.style.cssText = 'position:fixed;z-index:2147483647;right:16px;bottom:72px;padding:10px 14px;' +
          'border:0;border-radius:8px;background:#2563eb;color:#fff;font:13px sans-serif;cursor:pointer;';
        /* El resultado del clic queda en window.__usilCaptureResult para que el agente lo lea. */
        button.onclick = () => { button.remove(); capture().then((r) => { window.__usilCaptureResult = r; }); };
        document.body.appendChild(button);
      } catch (e) { /* sin interfaz disponible */ }
      return done({status: 'pending', reason: inbox ? 'permiso_carpeta_requiere_clic_del_usuario'
                                                    : 'bandeja_no_configurada_requiere_clic_del_usuario'});
    }
    try {
      if (inbox) {
        permission = await inbox.requestPermission({mode: 'readwrite'});
      } else {
        inbox = await window.showDirectoryPicker({id: 'usil-zoom-capture-inbox', mode: 'readwrite'});
        permission = await inbox.queryPermission({mode: 'readwrite'});
      }
    } catch (e) {
      return done({status: 'pending', reason: 'seleccion_de_carpeta_cancelada'});
    }
    if (permission !== 'granted') {
      return done({status: 'pending', reason: 'permiso_de_carpeta_denegado'});
    }
  }
  if (!(await hasMarker(inbox))) {
    return done({status: 'pending', reason: 'carpeta_sin_marcador_elige_la_bandeja_de_capturas'});
  }
  await stored(inbox).catch(() => null);
  const hex = async (text) => Array.from(new Uint8Array(
    await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))))
    .map((b) => b.toString(16).padStart(2, '0')).join('');
  /* Metadatos de la grabacion desde la solicitud play/info que el reproductor ya hizo.
     Solo se guardan hashes de los identificadores, la hora de inicio, la duracion y el tema. */
  const infoEntry = performance.getEntriesByType('resource').find((x) => {
    try {
      const u = new URL(x.name);
      return u.protocol === 'https:' && zoomHost(u.hostname) && u.pathname.startsWith('/nws/recording/1.0/play/info/');
    } catch (e) { return false; }
  });
  if (!infoEntry) {
    return done({status: 'pending', reason: 'metadatos_reproductor_no_observados_recargar'});
  }
  let info;
  try {
    const response = await fetch(infoEntry.name, {credentials: 'include', redirect: 'error', cache: 'force-cache'});
    info = (await response.json()).result;
  } catch (e) {
    return done({status: 'pending', reason: 'metadatos_reproductor_no_validos'});
  }
  if (!info || !info.recording || typeof info.recording.meetingId !== 'string' || typeof info.recording.id !== 'string' ||
      typeof info.fileStartTime !== 'number' || typeof info.duration !== 'number') {
    return done({status: 'pending', reason: 'metadatos_reproductor_no_validos'});
  }
  if (info.hasTranscript === false) {
    return done({status: 'pending', reason: 'grabacion_sin_transcripcion'});
  }
  const recording = {
    occurrence_sha256: await hex(info.recording.meetingId),
    file_id_sha256: await hex(info.recording.id),
    file_type: info.recording.videoFile === false ? 'M4A' : 'MP4',
    started_at_utc: new Date(info.fileStartTime).toISOString(),
    duration_seconds: Math.round(info.duration),
    topic: String((info.meet && info.meet.topic) || '').slice(0, 300),
  };
  const observed = new Map();
  for (const entry of performance.getEntriesByType('resource')) {
    let url;
    try { url = new URL(entry.name); } catch (e) { continue; }
    const fids = url.searchParams.getAll('fid');
    if (url.protocol === 'https:' && zoomHost(url.hostname) && url.pathname === '/rec/play/vtt' &&
        url.searchParams.getAll('type').join(',') === 'transcript' && fids.length === 1 && fids[0] &&
        !observed.has(fids[0])) {
      observed.set(fids[0], url.href);
    }
  }
  if (!observed.size) {
    return done({status: 'pending', reason: 'vtt_no_observado_abrir_panel_transcripcion_y_reintentar'});
  }
  const reference = {method: 'observed_viewer_path_sha256', sha256: await hex(location.pathname)};
  const files = [];
  for (const [fid, href] of observed) {
    let text;
    try {
      const response = await fetch(href, {credentials: 'include', redirect: 'error', cache: 'force-cache'});
      const type = (response.headers.get('content-type') || '').toLowerCase();
      if (!response.ok || type.includes('text/html')) {
        return done({status: 'pending', reason: 'respuesta_http_' + response.status});
      }
      text = await response.text();
    } catch (e) {
      return done({status: 'pending', reason: 'solicitud_vtt_fallida'});
    }
    if (!/^﻿?WEBVTT(\s|$)/.test(text)) {
      return done({status: 'pending', reason: 'respuesta_no_es_webvtt'});
    }
    const id = (await hex(fid)).slice(0, 20);
    const capture = {schema: 'zoom-vtt-capture', version: 2, id, recording_reference: reference, recording,
      captured_at: new Date().toISOString(), vtt: text};
    try {
      const file = await inbox.getFileHandle('zoom-vtt-' + id + '.json', {create: true});
      const stream = await file.createWritable();
      await stream.write(JSON.stringify(capture));
      await stream.close();
    } catch (e) {
      return done({status: 'pending', reason: 'escritura_en_bandeja_fallida'});
    }
    files.push({id, cues: (text.match(/-->/g) || []).length});
  }
  return done({status: 'captured', files});
})();
