"""Generate Windows multi-resolution icons from NovaMind's existing SVG mark.

Run with the project's Python before packaging. The current mark uses polygon
paths and a circle; unsupported SVG commands fail explicitly rather than render
an approximate logo. Pillow is already a project dependency.
"""
from __future__ import annotations

from pathlib import Path
import re
import xml.etree.ElementTree as ET

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]


def polygon_points(data):
    tokens = re.findall(r'[A-Za-z]|[-+]?(?:\d*\.\d+|\d+)', data)
    points, command, position, index = [], None, (0., 0.), 0
    while index < len(tokens):
        if tokens[index].isalpha():
            command = tokens[index]
            index += 1
            if command in ('z', 'Z'):
                break
        if command not in ('m', 'M', 'l', 'L'):
            raise ValueError(f'Unsupported SVG path command: {command}')
        x, y = float(tokens[index]), float(tokens[index+1])
        index += 2
        if command.islower():
            x, y = position[0]+x, position[1]+y
        points.append((x, y))
        position = x, y
        if command in ('m', 'M'):
            command = 'l' if command == 'm' else 'L'
    return points


def build_icon():
    source = ROOT / 'novamind/webui/static/nova-mark.svg'
    output = ROOT / 'novamind/desktop/assets'
    output.mkdir(parents=True, exist_ok=True)
    size = 1024
    image = Image.new('RGBA', (size, size))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((24, 24, size-25, size-25), radius=224, fill='#14213a')
    scale, offset = size * .76 / 64, size * .12
    palette = {'#14213a': '#edf3ff', '#f7faff': '#14213a'}
    for node in ET.parse(source).getroot():
        tag = node.tag.rsplit('}', 1)[-1]
        fill = palette.get(node.get('fill'), node.get('fill'))
        if tag == 'path':
            points = [(x*scale+offset, y*scale+offset) for x, y in polygon_points(node.get('d'))]
            draw.polygon(points, fill=fill)
            width = float(node.get('stroke-width', 0)) * scale
            if width:
                draw.line(points+[points[0]], fill=fill, width=round(width), joint='curve')
                for x, y in points:
                    r = width/2
                    draw.ellipse((x-r, y-r, x+r, y+r), fill=fill)
        elif tag == 'circle':
            x, y = float(node.get('cx'))*scale+offset, float(node.get('cy'))*scale+offset
            radius = float(node.get('r'))*scale
            draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=fill)
        else:
            raise ValueError(f'Unsupported SVG element: {tag}')
    preview = image.resize((256, 256), Image.Resampling.LANCZOS)
    preview.save(output / 'novamind.png')
    sizes = [(n, n) for n in (16, 20, 24, 32, 40, 48, 64, 128, 256)]
    image.save(output / 'novamind.ico', sizes=sizes)
    print(f'Generated NovaMind icon ({len(sizes)} resolutions): {output}')


if __name__ == '__main__':
    build_icon()
