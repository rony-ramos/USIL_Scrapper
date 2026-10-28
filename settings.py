"""Configuracion local, rutas relativas al archivo y errores accionables."""
import json
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BASE = Path(__file__).resolve().parent


def load_settings(path=None):
    path = Path(path or BASE / 'biblioteca.config.json').resolve()
    if not path.exists():
        raise ValueError('Falta configuracion local. Copia biblioteca.config.example.json a biblioteca.config.json y editalo.')
    cfg = json.loads(path.read_text(encoding='utf-8-sig'))
    url = cfg.get('canvas_url', '').rstrip('/')
    parts = urlsplit(url)
    if parts.scheme != 'https' or not parts.hostname or parts.path or parts.query or parts.username:
        raise ValueError('canvas_url debe ser un origen HTTPS, por ejemplo https://universidad.instructure.com')
    cfg['canvas_url'] = url
    for key, default in [('root', 'biblioteca'), ('knowledge_root', 'conocimiento'), ('browser_profile', '.browser-profile'),
                         ('courses_file', 'cursos_seleccionados.json'), ('pilot_file', 'piloto.json')]:
        value = Path(cfg.get(key, default))
        cfg[key] = str((path.parent / value).resolve() if not value.is_absolute() else value.resolve())
    cfg.setdefault('sharepoint_hosts', [])
    cfg.setdefault('download_hosts', [])
    cfg.setdefault('knowledge_timezone', 'America/Lima')
    cfg.setdefault('agents', {})
    if not isinstance(cfg['agents'], dict):
        raise ValueError('agents debe ser un objeto JSON')
    cfg['agents'].setdefault('command', 'agy')
    cfg['agents'].setdefault('model', 'gemini-3.8-flash-high')
    cfg['agents'].setdefault('timeout_seconds', 300)
    cfg['agents'].setdefault('transcript_chunk_size', 80)
    cfg['agents'].setdefault('pdf_renderer', 'pdftoppm')
    for key in ('command', 'model', 'pdf_renderer'):
        if not isinstance(cfg['agents'][key], str) or not cfg['agents'][key].strip():
            raise ValueError(f'agents.{key} debe ser texto no vacio')
    for key in ('timeout_seconds', 'transcript_chunk_size'):
        if type(cfg['agents'][key]) is not int or cfg['agents'][key] < 1:
            raise ValueError(f'agents.{key} debe ser un entero positivo')
    for key in ('command', 'pdf_renderer'):
        value = cfg['agents'][key]
        if '/' in value or '\\' in value:
            location = Path(value)
            cfg['agents'][key] = str((path.parent / location).resolve() if not location.is_absolute() else location.resolve())
    try:
        ZoneInfo(cfg['knowledge_timezone'])
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError('knowledge_timezone debe ser una zona IANA valida; instala tzdata en Windows') from None
    for host in cfg['sharepoint_hosts'] + cfg['download_hosts']:
        if not host or '/' in host or ':' in host or host.startswith('.'):
            raise ValueError('Configura hosts exactos, sin esquema, ruta ni comodines')
    return cfg


def load_pilot(cfg):
    path = Path(cfg['pilot_file'])
    if not path.exists():
        raise ValueError('Falta piloto.json; configura un recurso propio usando piloto.example.json')
    return json.loads(path.read_text(encoding='utf-8-sig'))
