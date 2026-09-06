"""Copia ejemplos editables sin tocar archivos existentes."""
from pathlib import Path

BASE = Path(__file__).resolve().parent


def initialize(base=BASE):
    for name in ('biblioteca.config', 'cursos_seleccionados', 'piloto'):
        target = base / f'{name}.json'
        try:
            with target.open('x', encoding='utf-8') as f:
                f.write((base / f'{name}.example.json').read_text('utf-8'))
            print(f'Creado: {target.name}')
        except FileExistsError:
            print(f'Conservado: {target.name}')


if __name__ == '__main__':
    initialize()
    print('Edita biblioteca.config.json y ejecuta canvas_sync.py --list-courses.')
