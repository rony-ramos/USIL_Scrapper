"""Configuracion local, rutas relativas al archivo y errores accionables."""
import json
from pathlib import Path
from urllib.parse import urlsplit

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
    for key, default in [('root', 'biblioteca'), ('browser_profile', '.browser-profile'),
                         ('courses_file', 'cursos_seleccionados.json'), ('pilot_file', 'piloto.json')]:
        value = Path(cfg.get(key, default))
        cfg[key] = str((path.parent / value).resolve() if not value.is_absolute() else value.resolve())
    cfg.setdefault('sharepoint_hosts', [])
    cfg.setdefault('download_hosts', [])
    for host in cfg['sharepoint_hosts'] + cfg['download_hosts']:
        if not host or '/' in host or ':' in host or host.startswith('.'):
            raise ValueError('Configura hosts exactos, sin esquema, ruta ni comodines')
    return cfg


def load_pilot(cfg):
    path = Path(cfg['pilot_file'])
    if not path.exists():
        raise ValueError('Falta piloto.json; configura un recurso propio usando piloto.example.json')
    return json.loads(path.read_text(encoding='utf-8-sig'))
