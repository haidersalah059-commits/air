"""Compile airline/translations_ar.py into locale/ar/LC_MESSAGES/django.mo.

Writes the .mo file directly (same format as GNU msgfmt), so no gettext tools are needed on Windows.
"""
import array
import struct
from pathlib import Path

from django.core.management.base import BaseCommand

from airline.translations_ar import AR

HEADER = (
    "Content-Type: text/plain; charset=UTF-8\n"
    "Language: ar\n"
    "Plural-Forms: nplurals=6; plural=n==0 ? 0 : n==1 ? 1 : n==2 ? 2 : n%100>=3 && n%100<=10 ? 3 : "
    "n%100>=11 && n%100<=99 ? 4 : 5;\n"
)


def write_mo(messages, path):
    keys = sorted(messages)
    ids = strs = b""
    offsets = []
    for key in keys:
        k, v = key.encode("utf-8"), messages[key].encode("utf-8")
        offsets.append((len(ids), len(k), len(strs), len(v)))
        ids += k + b"\0"
        strs += v + b"\0"
    key_start = 7 * 4 + 16 * len(keys)
    value_start = key_start + len(ids)
    key_offsets, value_offsets = [], []
    for o1, l1, o2, l2 in offsets:
        key_offsets += [l1, o1 + key_start]
        value_offsets += [l2, o2 + value_start]
    output = struct.pack("Iiiiiii", 0x950412DE, 0, len(keys), 7 * 4, 7 * 4 + len(keys) * 8, 0, 0)
    output += array.array("i", key_offsets).tobytes() + array.array("i", value_offsets).tobytes() + ids + strs
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(output)


class Command(BaseCommand):
    help = "Compile the Arabic translations into a .mo file."

    def handle(self, *args, **options):
        path = Path(__file__).resolve().parents[2] / "locale" / "ar" / "LC_MESSAGES" / "django.mo"
        write_mo({"": HEADER, **AR}, path)
        self.stdout.write(self.style.SUCCESS(f"{len(AR)} messages → {path}"))
