/* Captura de transcripciones Zoom desde el reproductor abierto por el usuario.
   Se ejecuta en la pagina /rec/play/ (marcador del navegador o agente con el navegador del usuario).
   - Solo reutiliza solicitudes VTT que el reproductor ya hizo; no construye URLs ni tokens.
   - El navegador agrega sus propias cookies; este script no las lee.
   - Descarga zoom-vtt-<id>.json con el VTT y dos hashes. No guarda URLs, cookies ni cabeceras.
   - Devuelve solo un resumen (id, segmentos), nunca el texto ni la URL.
   Sin comentarios de linea para que funcione como marcador javascript:. */
(async () => {
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
      ? 'Transcripcion capturada: ' + result.files.map((f) => f.id + ' (' + f.cues + ' segmentos)').join(', ')
      : 'Captura VTT pendiente: ' + result.reason);
    return result;
  };
  const zoomHost = (h) => h === 'zoom.us' || h.endsWith('.zoom.us');
  if (location.protocol !== 'https:' || !zoomHost(location.hostname) ||
      !location.pathname.startsWith('/rec/play/') || location.pathname === '/rec/play/vtt') {
    return done({status: 'pending', reason: 'abrir_desde_reproductor_zoom'});
  }
  const hex = async (text) => Array.from(new Uint8Array(
    await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))))
    .map((b) => b.toString(16).padStart(2, '0')).join('');
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
      const response = await fetch(href, {credentials: 'include', redirect: 'error', cache: 'no-store'});
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
    const capture = {schema: 'zoom-vtt-capture', version: 1, id, recording_reference: reference,
      captured_at: new Date().toISOString(), vtt: text};
    const link = document.createElement('a');
    link.href = URL.createObjectURL(new Blob([JSON.stringify(capture)], {type: 'application/json'}));
    link.download = 'zoom-vtt-' + id + '.json';
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 10000);
    files.push({id, cues: (text.match(/-->/g) || []).length});
  }
  return done({status: 'captured', files});
})();
