#!/usr/bin/env python3
"""
mkv_tool.py - Ferramenta para ficheiros MKV (usa FFmpeg/ffprobe e MKVToolNIX).

Comandos:
  extract      Extrai a faixa de áudio SEM perdas (stream copy)
  trim         Corta X tempo do início e Y tempo do fim (ou por capítulos)
  add-audio    Adiciona um ficheiro de áudio como nova faixa a um MKV existente
  detect-sync  Deteta onde o ficheiro 1 encaixa no ficheiro 2 (compara cortes de plano)
  workflow     Fluxo único com 2 ficheiros (usa --auto-sync para sincronizar sozinho):
                 ficheiro 1 -> extrai o áudio
                 ficheiro 2 -> localiza a secção central (define início/fim a retirar)
                 junta o áudio do 1 ao ficheiro 2 COMPLETO, alinhado com a secção central;
                 o início e o fim removidos são mantidos (usa --no-restore para os descartar)
                 Suporta --op1 e --op2 para criar um ficheiro modular sem aberturas embutidas,
                 ligado via Segment Linking e Ordered Chapters (requer mkvmerge/mkvinfo).
                 No modo modular, --end-cut também remove o encerramento do segmento do episódio.
                 Usa --opening para montar abertura + episódio dobrado + abertura, sem legendas.

A maioria das operações usa stream copy e preserva as faixas do destino.
O modo --opening é uma exceção: recodifica para montar a abertura + episódio +
abertura numa só faixa de vídeo/áudio e remove as restantes faixas e legendas.
Os ficheiros originais nunca são alterados.

Requisitos: Python 3.8+, ffmpeg + ffprobe no PATH (e mkvmerge + mkvinfo para --op1/--op2).

Exemplos:
  python mkv_tool.py extract filme.mkv
  python mkv_tool.py trim ep01.mkv --start-cut 1:49 --end-cut 2:13
  python mkv_tool.py add-audio filme.mkv --audio filme.ac3 --lang por

  # Fluxo único padrão
  python mkv_tool.py workflow fich1.mkv fich2.mkv --auto-sync --lang por --title "Português" --default

  # Fluxo modular (Opção 2 - Segment Linking / Ordered Chapters):
  python mkv_tool.py workflow pasta1/ pasta2/ --auto-sync --op1 OP_Original.mkv --op2 OP_Nova.mkv --lang por --title "Português"

  # Abertura no início e no fim, só com o áudio escolhido do ficheiro 1 no episódio:
  python new_mkv_tool.py workflow audio.mkv episodio.mkv --auto-sync --opening abertura2.mkv --lang por --title "Português"

  # Áudio AC3 já sincronizado: cortar opening/ending com os mesmos tempos do vídeo:
  python new_mkv_tool.py workflow audio.ac3 episodio.mkv --audio-only --start-cut 1:49 --end-cut 2:08 --opening abertura2.mkv --lang por

 # Bulk: sincroniza o primeiro episódio de cada grupo de 10 e reutiliza o timestamp:
 python new_mkv_tool.py workflow ".\\pasta_audio" ".\\pasta_video" --out ".\\resultado" --auto-sync --no-restore --end-cut 2 --post-sync-start-cut 0.30 --limit-episodes 100 --start-episode 1 --lang por --title "Português" --default --yes
"""

from __future__ import annotations

import argparse
import bisect
import json
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path
import xml.etree.ElementTree as ET

# Codecs cujo ficheiro "cru" é seguro com stream copy.
# Tudo o resto (dts, truehd, ...) vai para .mka (Matroska Audio) para não perder nada.
CODEC_EXT = {
    "aac": ".m4a",
    "ac3": ".ac3",
    "eac3": ".eac3",
    "flac": ".flac",
    "opus": ".opus",
    "vorbis": ".ogg",
    "mp3": ".mp3",
    "alac": ".m4a",
}
AUDIO_EXTS = (".mka", ".m4a", ".ac3", ".eac3", ".flac", ".opus", ".ogg", ".mp3", ".wav", ".dts", ".thd", ".aac")


# ----------------------------------------------------------------- utilidades
def check_tools(require_mkvtoolnix: bool = False):
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"Erro: '{tool}' não encontrado no PATH. Instala o FFmpeg primeiro.")
    if require_mkvtoolnix:
        for tool in ("mkvmerge", "mkvinfo"):
            if not shutil.which(tool):
                sys.exit(f"Erro: '{tool}' não encontrado no PATH. Instala o MKVToolNIX para usar --op1/--op2.")


def probe(path: Path) -> dict:
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", "-show_chapters", str(path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"ffprobe falhou: {res.stderr.strip()}")
    return json.loads(res.stdout)


def audio_streams(info: dict) -> list:
    return [s for s in info["streams"] if s.get("codec_type") == "audio"]


def count_types(info: dict) -> dict:
    counts = {}
    for s in info["streams"]:
        t = s.get("codec_type", "other")
        counts[t] = counts.get(t, 0) + 1
    return counts


def run_ffmpeg(args: list):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stats"] + args
    res = subprocess.run(cmd)
    if res.returncode != 0:
        raise RuntimeError("ffmpeg terminou com erro")


def run_cmd(cmd: list, err_prefix: str = "Comando"):
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"{err_prefix} falhou: {res.stderr.strip() or res.stdout.strip()}")
    return res.stdout


def parse_time(text: str) -> float:
    """'1:49' -> 109.0 ; '00:02:13.5' -> 133.5 ; '109' -> 109.0"""
    total = 0.0
    for part in text.strip().split(":"):
        total = total * 60 + float(part)
    return total


def fmt_time(seconds: float) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def fmt_time_ns(seconds: float) -> str:
    """Formata segundos com precisão de nanossegundos para XML de capítulos Matroska."""
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:012.9f}"


def collect(path: Path, exts: tuple, recursive: bool) -> list:
    if path.is_file():
        return [path]
    if not path.is_dir():
        sys.exit(f"Erro: '{path}' não existe.")
    pattern = "**/*" if recursive else "*"
    return sorted(p for p in path.glob(pattern) if p.suffix.lower() in exts)


def pick_audio_index(info: dict, track: int, lang):
    streams = audio_streams(info)
    if not streams:
        raise RuntimeError("o ficheiro não tem faixas de áudio")
    if lang:
        for i, s in enumerate(streams):
            if s.get("tags", {}).get("language", "").lower() == lang.lower():
                return i, s
        raise RuntimeError(f"não há faixa de áudio com idioma '{lang}'")
    if track >= len(streams):
        raise RuntimeError(f"só existem {len(streams)} faixas de áudio (pedida: {track})")
    return track, streams[track]


def ensure_free(dest: Path, src: Path, overwrite: bool):
    if dest.resolve() == src.resolve():
        raise RuntimeError("o destino é igual à origem; usa --out ou --suffix")
    if dest.exists() and not overwrite:
        raise RuntimeError(f"'{dest.name}' já existe (usa --overwrite)")


def report(results: list):
    ok = [r for r in results if r[1]]
    bad = [r for r in results if not r[1]]
    print(f"\n=== Resumo: {len(ok)} OK, {len(bad)} com erro ===")
    for name, _, msg in bad:
        print(f"  [ERRO] {name}: {msg}")


# -------------------------------------------- utilidades MKVToolNIX / Segment Linking
def get_segment_uid(mkv_path: Path) -> str:
    """Obtém o Segment UID (16 bytes em hex) de um ficheiro MKV via mkvinfo."""
    out = run_cmd(["mkvinfo", str(mkv_path)], f"mkvinfo ({mkv_path.name})")
    # Aceita "0x12 0x34 ...", "12 34 ...", hexadecimal contínuo e UID no mesmo formato do mkvinfo.
    m = re.search(r"Segment UID:\s*(.+)", out, re.IGNORECASE)
    if not m:
        raise RuntimeError(f"Não foi possível extrair Segment UID de '{mkv_path.name}'")
    raw = m.group(1).strip()
    normalized = re.sub(r"0x", "", raw, flags=re.IGNORECASE)
    normalized = re.sub(r"[^0-9a-fA-F]", "", normalized)
    if len(normalized) != 32:
        raise RuntimeError(f"Segment UID inválido em '{mkv_path.name}': {raw}")
    return normalized.lower()


def generate_segment_uid() -> str:
    """Gera um Segment UID aleatório de 16 bytes (32 caracteres hex)."""
    return "".join(f"{random.randint(0, 255):02x}" for _ in range(16))


def format_uid_for_mkvmerge(uid_hex: str) -> str:
    """Normaliza um Segment UID para o formato hexadecimal contínuo do mkvmerge."""
    normalized = re.sub(r"[^0-9a-fA-F]", "", uid_hex)
    if len(normalized) != 32:
        raise ValueError("Segment UID deve ter exatamente 16 bytes (32 caracteres hex)")
    return normalized.lower()


def build_ordered_chapters_xml(
    op1_uid: str,
    op1_dur: float,
    op1_title: str,
    op2_uid: str,
    op2_dur: float,
    op2_title: str,
    ep_uid: str,
    ep_dur: float,
    xml_dest: Path,
):
    """
    Cria um ficheiro XML de Capítulos Ordenados (Ordered Chapters) com duas Edições:
      - Edição 1 (Default): OP 1 (Segment UID op1_uid) -> Episódio (Segment UID ep_uid)
      - Edição 2:           OP 2 (Segment UID op2_uid) -> Episódio (Segment UID ep_uid)
    """
    root = ET.Element("Chapters")

    def add_edition(edition_uid: str, is_default: bool, op_uid: str, op_d: float, op_name: str):
        ed = ET.SubElement(root, "EditionEntry")
        ET.SubElement(ed, "EditionUID").text = edition_uid
        ET.SubElement(ed, "EditionFlagHidden").text = "0"
        ET.SubElement(ed, "EditionFlagDefault").text = "1" if is_default else "0"
        ET.SubElement(ed, "EditionFlagOrdered").text = "1"

        # Capítulo 1: Abertura externa (Segment Linking)
        c1 = ET.SubElement(ed, "ChapterAtom")
        ET.SubElement(c1, "ChapterUID").text = str(random.randint(100000000, 9999999999))
        ET.SubElement(c1, "ChapterTimeStart").text = "00:00:00.000000000"
        ET.SubElement(c1, "ChapterTimeEnd").text = fmt_time_ns(op_d)
        ET.SubElement(c1, "ChapterFlagHidden").text = "0"
        ET.SubElement(c1, "ChapterFlagEnabled").text = "1"
        seg1 = ET.SubElement(c1, "ChapterSegmentUID", format="hex")
        seg1.text = op_uid
        disp1 = ET.SubElement(c1, "ChapterDisplay")
        ET.SubElement(disp1, "ChapterString").text = op_name
        ET.SubElement(disp1, "ChapterLanguage").text = "und"

        # Capítulo 2: Corpo do Episódio (Segmento atual / ep_uid)
        c2 = ET.SubElement(ed, "ChapterAtom")
        ET.SubElement(c2, "ChapterUID").text = str(random.randint(100000000, 9999999999))
        ET.SubElement(c2, "ChapterTimeStart").text = "00:00:00.000000000"
        ET.SubElement(c2, "ChapterTimeEnd").text = fmt_time_ns(ep_dur)
        ET.SubElement(c2, "ChapterFlagHidden").text = "0"
        ET.SubElement(c2, "ChapterFlagEnabled").text = "1"
        seg2 = ET.SubElement(c2, "ChapterSegmentUID", format="hex")
        seg2.text = ep_uid
        disp2 = ET.SubElement(c2, "ChapterDisplay")
        ET.SubElement(disp2, "ChapterString").text = "Episódio"
        ET.SubElement(disp2, "ChapterLanguage").text = "und"

    add_edition(str(random.randint(100000000, 9999999999)), True, op1_uid, op1_dur, op1_title)
    add_edition(str(random.randint(100000000, 9999999999)), False, op2_uid, op2_dur, op2_title)

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    with open(xml_dest, "wb") as f:
        f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write(b'<!DOCTYPE Chapters SYSTEM "matroskachapters.dtd">\n')
        tree.write(f, encoding="utf-8", xml_declaration=False)


def remux_with_ordered_chapters(
    src_mkv: Path,
    final_mkv: Path,
    ep_uid: str,
    chapters_xml: Path,
    op1_uid: str,
):
    """Empacota o MKV final com Segment UID conhecido, capítulo ligado e prev-segment-uid."""
    cmd = [
        "mkvmerge",
        "-o", str(final_mkv),
        "--segment-uid", format_uid_for_mkvmerge(ep_uid),
        "--link-to-previous", format_uid_for_mkvmerge(op1_uid),
        "--chapters", str(chapters_xml),
        str(src_mkv),
    ]
    run_cmd(cmd, "mkvmerge (Ordered Chapters)")


# ------------------------------------------------------- operações principais
def extract_audio(src: Path, dest_dir: Path, track=0, lang=None, container="native", overwrite=False):
    """Extrai uma faixa de áudio sem perdas. Devolve (caminho_do_audio, info_da_stream)."""
    info = probe(src)
    idx, stream = pick_audio_index(info, track, lang)
    codec = stream["codec_name"]
    if container == "mka":
        ext = ".mka"
    elif codec.startswith("pcm_"):
        ext = ".wav"
    else:
        ext = CODEC_EXT.get(codec, ".mka")

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (src.stem + ext)
    if dest.exists() and not overwrite:
        raise RuntimeError(f"'{dest.name}' já existe (usa --overwrite)")

    print(f"   faixa de áudio {idx}: {codec} -> {dest.name}")
    run_ffmpeg(["-y", "-i", str(src), "-map", f"0:a:{idx}", "-vn", "-sn", "-dn", "-c:a", "copy", str(dest)])
    return dest, stream


def compute_cut(info: dict, start_cut=None, end_cut=None, start_chapter=None, end_chapter=None):
    """Devolve (início, fim) em segundos da secção a MANTER."""
    duration = float(info["format"]["duration"])

    if start_chapter is not None or end_chapter is not None:
        chapters = info.get("chapters", [])
        if not chapters:
            raise RuntimeError("o ficheiro não tem capítulos")
        n = len(chapters)
        sc = start_chapter or 1
        ec = end_chapter or n
        if not (1 <= sc <= ec <= n):
            raise RuntimeError(f"capítulos inválidos (o ficheiro tem {n})")
        start = float(chapters[sc - 1]["start_time"])
        end = float(chapters[ec - 1]["end_time"])
    else:
        start = parse_time(start_cut) if start_cut else 0.0
        end = duration - (parse_time(end_cut) if end_cut else 0.0)

    if end <= start:
        raise RuntimeError("o corte resulta num vídeo vazio (início >= fim)")
    return start, end


def trim_video(src: Path, dest: Path, start_cut=None, end_cut=None, start_chapter=None,
               end_chapter=None, precise=False, crf=14, ten_bit=False, overwrite=False):
    """Corta o vídeo mantendo TODAS as faixas (áudio, legendas, fontes) e capítulos."""
    info = probe(src)
    start, end = compute_cut(info, start_cut, end_cut, start_chapter, end_chapter)

    dest.parent.mkdir(parents=True, exist_ok=True)
    ensure_free(dest, src, overwrite)
    print(f"   corte: {fmt_time(start)} -> {fmt_time(end)} (duração {fmt_time(end - start)})")

    cmd = ["-y", "-i", str(src), "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
           "-map", "0", "-map_chapters", "0", "-ignore_unknown"]
    if precise:
        # Recodifica só o vídeo (corte ao frame); áudio, legendas e anexos continuam intactos
        cmd += ["-c", "copy", "-c:v", "libx264", "-crf", str(crf), "-preset", "slow",
                "-pix_fmt", "yuv420p10le" if ten_bit else "yuv420p"]
    else:
        cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
    cmd.append(str(dest))
    run_ffmpeg(cmd)
    return dest


def trim_video_segment(src: Path, dest: Path, start: float, end: float):
    """Extrai apenas o segmento de vídeo entre dois timestamps, sem outras faixas."""
    if end <= start:
        raise RuntimeError("o corte resulta num vídeo vazio (início >= fim)")
    dest.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg([
        "-y", "-i", str(src), "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
        "-map", "0:v:0", "-an", "-sn", "-dn", "-map_metadata", "-1",
        "-map_chapters", "-1", "-c:v", "copy", "-avoid_negative_ts", "make_zero",
        str(dest),
    ])
    return dest


def trim_audio_segment(src: Path, dest: Path, start: float, end: float):
    """Corta uma faixa de áudio entre timestamps relativos, preservando o codec."""
    if end <= start:
        raise RuntimeError("o corte resulta num áudio vazio (início >= fim)")
    dest.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg([
        "-y", "-i", str(src), "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
        "-map", "0:a:0", "-vn", "-sn", "-dn", "-c:a", "copy",
        "-avoid_negative_ts", "make_zero", str(dest),
    ])
    return dest


def mux_video_audio_only(video: Path, audio: Path, dest: Path, lang=None, title=None, offset=0.0):
    """Muxa o áudio sincronizado sobre o vídeo original, sem outras faixas."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["-y", "-i", str(video)]
    if offset:
        cmd += ["-itsoffset", f"{offset:.3f}"]
    cmd += [
        "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
        "-map_metadata", "0", "-map_chapters", "-1", "-sn", "-dn", "-c", "copy",
    ]
    if lang:
        cmd += ["-metadata:s:a:0", f"language={lang}"]
    if title:
        cmd += ["-metadata:s:a:0", f"title={title}"]
    cmd.append(str(dest))
    run_ffmpeg(cmd)
    return dest


def trim_video_audio_segment(src: Path, dest: Path, start: float, end: float):
    """Corta vídeo e áudio juntos para preservar a sincronização entre as faixas."""
    if end <= start:
        raise RuntimeError("o corte resulta num segmento vazio (início >= fim)")
    dest.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg([
        "-y", "-i", str(src), "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
        "-map", "0:v:0", "-map", "0:a:0", "-map_metadata", "0",
        "-map_chapters", "-1", "-sn", "-dn", "-c", "copy", "-avoid_negative_ts", "make_zero",
        str(dest),
    ])
    return dest


def compose_opening_loop(opening: Path, body: Path, dest: Path, lang=None, title=None):
    """Junta abertura + corpo + abertura numa única faixa de vídeo e áudio."""
    opening_info, body_info = probe(opening), probe(body)
    opening_audio = audio_streams(opening_info)
    body_audio = audio_streams(body_info)
    videos = [s for s in body_info["streams"] if s.get("codec_type") == "video"]
    if not opening_audio:
        raise RuntimeError(f"'{opening.name}' não tem faixa de áudio para usar nas aberturas")
    if len(body_audio) != 1 or len(videos) != 1:
        raise RuntimeError("o segmento do episódio deve ter exatamente uma faixa de vídeo e uma de áudio")

    width, height = int(videos[0]["width"]), int(videos[0]["height"])
    frame_rate = videos[0].get("avg_frame_rate") or videos[0].get("r_frame_rate") or "24000/1001"
    filters = []
    concat_inputs = []
    for i in range(3):
        source = i
        filters.append(
            f"[{source}:v:0]setpts=PTS-STARTPTS,fps={frame_rate},"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p[v{i}]"
        )
        filters.append(
            f"[{source}:a:0]aresample=48000:async=1:first_pts=0,"
            f"aformat=sample_rates=48000:channel_layouts=stereo,"
            f"asetpts=PTS-STARTPTS[a{i}]"
        )
        concat_inputs.extend((f"[v{i}]", f"[a{i}]"))
    filters.append("".join(concat_inputs) + "concat=n=3:v=1:a=1[outv][outa]")

    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "-y", "-i", str(opening), "-i", str(body), "-i", str(opening),
        "-filter_complex", ";".join(filters),
        "-map", "[outv]", "-map", "[outa]", "-map_metadata", "-1", "-map_chapters", "-1",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
    ]
    if lang:
        cmd += ["-metadata:s:a:0", f"language={lang}"]
    if title:
        cmd += ["-metadata:s:a:0", f"title={title}"]
    cmd.append(str(dest))
    run_ffmpeg(cmd)
    return dest


def mux_audio(video: Path, audio: Path, dest: Path, lang=None, title=None,
              default=False, offset=0.0, overwrite=False):
    """Adiciona 'audio' como nova faixa a 'video', preservando tudo o que já existe."""
    info = probe(video)
    n = len(audio_streams(info))  # a nova faixa será a de índice n

    dest.parent.mkdir(parents=True, exist_ok=True)
    ensure_free(dest, video, overwrite)
    print(f"   + {audio.name} como faixa de áudio #{n}" + (f" (offset {offset:+.3f}s)" if offset else ""))

    cmd = ["-y", "-i", str(video)]
    if offset:
        cmd += ["-itsoffset", f"{offset:.3f}"]
    cmd += ["-i", str(audio), "-map", "0", "-map", "1:a:0",
            "-map_chapters", "0", "-ignore_unknown", "-c", "copy"]
    if lang:
        cmd += [f"-metadata:s:a:{n}", f"language={lang}"]
    if title:
        cmd += [f"-metadata:s:a:{n}", f"title={title}"]
    if default:
        for i in range(n):
            cmd += [f"-disposition:a:{i}", "0"]
        cmd += [f"-disposition:a:{n}", "default"]
    cmd.append(str(dest))
    run_ffmpeg(cmd)
    return dest


# ------------------------------------------------------------------- comandos
def cmd_extract(args):
    files = collect(Path(args.input), (".mkv",), args.recursive)
    out_dir = Path(args.out) if args.out else None
    results = []
    for f in files:
        print(f"\n>> {f.name}")
        try:
            extract_audio(f, out_dir or f.parent, args.track, args.lang, args.container, args.overwrite)
            results.append((f.name, True, ""))
        except Exception as e:
            print(f"   ERRO: {e}")
            results.append((f.name, False, str(e)))
    report(results)


def cmd_trim(args):
    if args.start_chapter is None and args.end_chapter is None and args.start_cut is None and args.end_cut is None:
        sys.exit("Erro: indica --start-cut/--end-cut ou --start-chapter/--end-chapter.")
    files = collect(Path(args.input), (".mkv",), args.recursive)
    out_dir = Path(args.out) if args.out else None
    results = []
    for f in files:
        print(f"\n>> {f.name}")
        try:
            dest = (out_dir or f.parent) / (f.stem + args.suffix + ".mkv")
            trim_video(f, dest, args.start_cut, args.end_cut, args.start_chapter, args.end_chapter,
                       args.precise, args.crf, args.ten_bit, args.overwrite)
            results.append((f.name, True, ""))
        except Exception as e:
            print(f"   ERRO: {e}")
            results.append((f.name, False, str(e)))
    report(results)


def cmd_add_audio(args):
    videos = collect(Path(args.input), (".mkv",), args.recursive)
    audio_arg = Path(args.audio)
    out_dir = Path(args.out) if args.out else None
    results = []
    for v in videos:
        print(f"\n>> {v.name}")
        try:
            if audio_arg.is_file():
                audio = audio_arg
            else:
                candidates = [p for p in audio_arg.iterdir()
                              if p.is_file() and p.stem == v.stem and p.suffix.lower() in AUDIO_EXTS]
                if not candidates:
                    raise RuntimeError(f"não encontrei áudio com o nome '{v.stem}' em {audio_arg}")
                audio = candidates[0]
            dest = (out_dir or v.parent) / (v.stem + args.suffix + ".mkv")
            mux_audio(v, audio, dest, args.lang, args.title, args.default, args.offset, args.overwrite)
            results.append((v.name, True, ""))
        except Exception as e:
            print(f"   ERRO: {e}")
            results.append((v.name, False, str(e)))
    report(results)


# ------------------------------------------------- deteção automática de sincronização
def scene_cuts(path: Path, threshold: float = 0.3, length=None) -> list:
    """Instantes (s) das mudanças de plano. Não depende das cores nem da proporção do ecrã."""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-nostats"]
    if length:
        cmd += ["-t", f"{length:.3f}"]
    cmd += ["-i", str(path), "-map", "0:v:0", "-an", "-sn", "-dn",
            "-vf", f"scale=320:180,select='gt(scene,{threshold})',showinfo",
            "-f", "null", "-"]
    res = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if res.returncode != 0:
        raise RuntimeError("ffmpeg falhou na deteção de planos: " + res.stderr[-300:])
    times = [float(m) for m in re.findall(r"pts_time:\s*([0-9]+\.?[0-9]*)", res.stderr)]
    out = []
    for t in sorted(times):
        if t < 0.3:
            continue
        if out and t - out[-1] < 0.25:  # junta planos quase coincidentes (fades, flashes)
            continue
        out.append(t)
    return out


def _match_offset(c1: list, c2: list, ratio: float, tol: float):
    c2 = sorted(c2)
    s1 = [t * ratio for t in c1]

    def near(x):
        i = bisect.bisect_left(c2, x)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(c2):
                d = c2[j] - x
                if best is None or abs(d) < abs(best):
                    best = d
        return best

    scores = {}
    for a in s1[:40]:
        for b in c2:
            d = round(b - a, 2)
            if d < -tol or d in scores:
                continue
            n = 0
            for x in s1:
                e = near(x + d)
                if e is not None and abs(e) <= tol:
                    n += 1
            scores[d] = n
    if not scores:
        return None
    best_d = max(scores, key=scores.get)
    best_n = scores[best_d]
    second = max((n for d, n in scores.items() if abs(d - best_d) > 1.0), default=0)

    diffs = []
    for x in s1:
        e = near(x + best_d)
        if e is not None and abs(e) <= tol:
            diffs.append(best_d + e)
    if not diffs:
        return None
    offset = statistics.median(diffs)
    return {
        "offset": offset, "ratio": ratio, "matched": len(diffs), "total": len(c1),
        "second": second, "resid": max(abs(v - offset) for v in diffs),
        "ok": (best_n >= 5
               and best_n >= 0.6 * len(c1)
               and best_n >= 2 * second),
    }


def detect_sync(f1: Path, f2: Path, threshold=0.3, length=240.0, search=600.0, tol=0.12) -> dict:
    """Descobre em que instante do ficheiro 2 começa o conteúdo do ficheiro 1."""
    print(f"   a analisar planos do ficheiro 1 (primeiros {length:.0f}s)...")
    c1 = scene_cuts(f1, threshold, length)
    print(f"   a analisar planos do ficheiro 2 (primeiros {length + search:.0f}s)...")
    c2 = scene_cuts(f2, threshold, length + search)
    print(f"   cortes detetados: {len(c1)} (ficheiro 1) / {len(c2)} (ficheiro 2)")
    if len(c1) < 5:
        raise RuntimeError("poucos cortes de plano no ficheiro 1; tenta baixar --threshold (ex: 0.2) "
                           "ou aumentar --detect-length")
    if len(c2) < 5:
        raise RuntimeError("poucos cortes de plano no ficheiro 2; tenta baixar --threshold")

    best = None
    for r in (1.0, 25 / 23.976, 23.976 / 25, 25 / 24, 24 / 25):
        res = _match_offset(c1, c2, r, tol)
        if res and (best is None or res["matched"] > best["matched"]):
            best = res
        if res and res["ok"]:
            best = res
            break
    if best is None or not best["ok"]:
        got = f"{best['matched']}/{best['total']}" if best else "0"
        raise RuntimeError(
            f"não consegui sincronizar com confiança (cortes coincidentes: {got}). "
            "Tenta --threshold diferente, aumenta --detect-search se a abertura for longa, "
            "ou indica o ponto manualmente com --start-cut.")
    if abs(best["ratio"] - 1.0) > 1e-6:
        raise RuntimeError(
            f"os vídeos parecem ter velocidades diferentes (fator {best['ratio']:.4f}, "
            "ex: 25 fps vs 23.976 fps). Corrigir isso exigiria recodificar o áudio, "
            "o que este programa não faz.")

    print(f"   sincronização: o áudio entra aos {fmt_time(best['offset'])} do ficheiro 2 "
          f"({best['matched']}/{best['total']} cortes coincidem, desvio máx. {best['resid'] * 1000:.0f} ms)")
    return best


def cmd_detect_sync(args):
    res = detect_sync(Path(args.file1), Path(args.file2), args.threshold, args.detect_length, args.detect_search)
    print(f"\nOffset: {res['offset']:.3f} s  ({fmt_time(res['offset'])})")
    print("Usa-o com:  --start-cut", f"{res['offset']:.3f}", "  (ou simplesmente --auto-sync no workflow)")


# --------------------------------------------------------------------- workflow
def pick_file(title: str) -> Path:
    """Abre uma janela para escolher um ficheiro (ou pede o caminho no terminal)."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = filedialog.askopenfilename(
            title=title, filetypes=[("Matroska", "*.mkv"), ("Todos os ficheiros", "*.*")])
        root.destroy()
    except Exception:
        chosen = input(f"{title}\nCaminho: ").strip().strip('"')
    if not chosen:
        sys.exit("Cancelado.")
    return Path(chosen)


def ask_cuts(args):
    if args.auto_sync:
        return
    nothing = (args.start_cut is None and args.end_cut is None
               and args.start_chapter is None and args.end_chapter is None)
    if nothing and sys.stdin.isatty():
        s = input("Tempo a remover do INÍCIO do ficheiro 2 (ex: 1:49, vazio = nenhum): ").strip()
        e = input("Tempo a remover do FIM do ficheiro 2 (ex: 2:13, vazio = nenhum): ").strip()
        args.start_cut = s or None
        args.end_cut = e or None
    if (args.start_cut is None and args.end_cut is None
            and args.start_chapter is None and args.end_chapter is None):
        sys.exit("Erro: indica --start-cut/--end-cut ou --start-chapter/--end-chapter.")


def run_pair(
    f1: Path,
    f2: Path,
    args,
    op_meta: dict | None = None,
    sync_offset: float | None = None,
) -> Path:
    out_dir = Path(args.out) if args.out else f2.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / (f2.stem + args.suffix + ".mkv")
    if final.exists() and not args.overwrite:
        raise RuntimeError(f"'{final.name}' já existe (usa --overwrite)")

    modular_mode = op_meta is not None
    opening_loop_mode = args.opening is not None
    audio_only_mode = args.audio_only
    restore = (not args.no_restore) and (not modular_mode) and not opening_loop_mode
    tmp = Path(tempfile.mkdtemp(prefix="mkvtool_", dir=str(out_dir)))
    try:
        print("\n[1/4] A extrair o áudio do ficheiro 1..." if modular_mode else
              "\n[1/3] A extrair o áudio do ficheiro 1...")
        audio, stream = extract_audio(f1, tmp, args.src_track, args.src_lang, args.container, True)
        d_audio = float(probe(audio)["format"]["duration"])
        audio_start_trim = 0.0

        if args.auto_sync:
            if sync_offset is None:
                print("[2/4] A detetar automaticamente onde o áudio encaixa no ficheiro 2..." if modular_mode
                      else "[2/3] A detetar automaticamente onde o áudio encaixa no ficheiro 2...")
            else:
                print(f"   a aplicar o timestamp de sincronização do lote: {fmt_time(sync_offset)}")
            dur2 = float(probe(f2)["format"]["duration"])
            sync_start = sync_offset
            if sync_start is None:
                sync_start = detect_sync(
                    f1, f2, args.threshold, args.detect_length, args.detect_search
                )["offset"]
            start = sync_start + args.post_sync_start_cut
            audio_start_trim = args.post_sync_start_cut
            audio_end = sync_start + args.offset + d_audio
            if modular_mode or not restore:
                print(f"   fim previsto do áudio do ficheiro 1 no ficheiro 2: {fmt_time(audio_end)}")
                end_cut = parse_time(args.end_cut) if args.end_cut else 0.0
                end = min(dur2, audio_end) - end_cut
            else:
                end = min(dur2, start + d_audio)
            if end <= start:
                raise RuntimeError("o corte resulta num vídeo vazio (início >= fim)")
            end_cut = max(0.0, dur2 - end)
            cut_args = (f"{start:.3f}", f"{end_cut:.3f}", None, None)
        else:
            step = "[2/4]" if modular_mode else "[2/3]"
            print(f"{step} A localizar a secção central do ficheiro 2..." if restore
                  else f"{step} A cortar o ficheiro 2...")
            cut_args = (args.start_cut, args.end_cut, args.start_chapter, args.end_chapter)
            info2 = probe(f2)
            start, end = compute_cut(info2, *cut_args)
            if (not restore and not audio_only_mode and args.start_cut is not None
                    and args.start_chapter is None and args.end_chapter is None):
                audio_end = start + args.offset + d_audio
                print(f"   fim previsto do áudio do ficheiro 1 no ficheiro 2: {fmt_time(audio_end)}")
                end_cut = parse_time(args.end_cut) if args.end_cut else 0.0
                end = min(float(info2["format"]["duration"]), audio_end) - end_cut
                if end <= start:
                    raise RuntimeError("o corte resulta num vídeo vazio (início >= fim)")
                end_cut = max(0.0, float(info2["format"]["duration"]) - end)
                cut_args = (args.start_cut, f"{end_cut:.3f}", None, None)
            if not audio_only_mode and abs((end - start) - d_audio) > 2:
                print(f"   AVISO: a secção de vídeo dura {fmt_time(end - start)} e o áudio dura {fmt_time(d_audio)}.")
                print("          Pode estar dessincronizado. Usa --auto-sync, ajusta o corte ou usa --offset.")

        if restore:
            print(f"   o áudio novo entra aos {fmt_time(start)} (início e fim originais são mantidos)")
            video_src = f2
            offset = start + args.offset  # o áudio novo entra onde começa a secção central
        elif audio_only_mode:
            tags = stream.get("tags", {})
            lang = args.lang or tags.get("language")
            title = args.title or tags.get("title")
            print("   a juntar primeiro o áudio sincronizado ao vídeo original...")
            muxed_full = mux_video_audio_only(
                f2, audio, tmp / (f2.stem + "_audio_muxed_full.mkv"),
                lang, title, args.offset,
            )
            video_src = trim_video_audio_segment(
                muxed_full, tmp / (f2.stem + "_body_av.mkv"), start, end
            )
            offset = 0.0
        elif opening_loop_mode:
            video_src = trim_video_segment(
                f2, tmp / (f2.stem + "_body_video.mkv"), start, end
            )
            offset = args.offset
        else:
            video_src = trim_video(f2, tmp / (f2.stem + "_cut.mkv"), *cut_args,
                                   args.precise, args.crf, args.ten_bit, True)
            offset = args.offset

        if not restore and not audio_only_mode:
            audio_duration = end - start - offset
            if audio_duration <= 0:
                raise RuntimeError("o offset deixa o segmento sem duração de áudio")
            audio_end_trim = min(d_audio, audio_start_trim + audio_duration)
            if audio_start_trim >= audio_end_trim:
                raise RuntimeError("o corte remove toda a faixa de áudio do ficheiro 1")
            if audio_start_trim > 0 or d_audio > audio_duration + 0.05:
                trimmed_audio = tmp / (audio.stem + "_cut" + audio.suffix)
                audio = trim_audio_segment(
                    audio, trimmed_audio, audio_start_trim, audio_end_trim
                )

        step_mux = "[3/4]" if modular_mode or opening_loop_mode else "[3/3]"
        print(f"{step_mux} A juntar o áudio ao vídeo...")
        tags = stream.get("tags", {})
        lang = args.lang or tags.get("language")
        title = args.title or tags.get("title")

        mux_dest = (tmp / (f2.stem + "_muxed.mkv")) if modular_mode or opening_loop_mode else final
        if audio_only_mode:
            print(f"{step_mux} O áudio já foi muxado ao original antes do corte.")
            mux_dest = video_src
        else:
            print(f"{step_mux} A juntar o áudio ao vídeo...")
            mux_audio(video_src, audio, mux_dest, lang, title, args.default, offset, True)

        if opening_loop_mode:
            print("[4/4] A juntar abertura + episódio + abertura (sem outras faixas nem legendas)...")
            print("   O vídeo e o áudio serão recodificados para garantir segmentos compatíveis.")
            compose_opening_loop(Path(args.opening), mux_dest, final, lang, title)

        if modular_mode:
            print("[4/4] A configurar Segment Linking e Capítulos Ordenados (mkvmerge)...")
            ep_dur = float(probe(mux_dest)["format"]["duration"])
            ep_uid = generate_segment_uid()
            xml_path = tmp / "ordered_chapters.xml"
            build_ordered_chapters_xml(
                op1_uid=op_meta["op1_uid"],
                op1_dur=op_meta["op1_dur"],
                op1_title=args.op1_title,
                op2_uid=op_meta["op2_uid"],
                op2_dur=op_meta["op2_dur"],
                op2_title=args.op2_title,
                ep_uid=ep_uid,
                ep_dur=ep_dur,
                xml_dest=xml_path,
            )
            remux_with_ordered_chapters(mux_dest, final, ep_uid, xml_path, op_meta["op1_uid"])
            print(f"   Segment UID atribuído ao episódio: {ep_uid}")
            print(f"   Edições criadas: '{args.op1_title}' (predefinida) e '{args.op2_title}'")

        if args.keep_temp:
            print(f"   Ficheiros intermédios mantidos em: {tmp}")
    finally:
        if not args.keep_temp:
            shutil.rmtree(tmp, ignore_errors=True)

    info_final = probe(final)
    if opening_loop_mode:
        counts = count_types(info_final)
        if counts != {"video": 1, "audio": 1}:
            raise RuntimeError(f"resultado inesperado: faixas finais {counts}")
        print("\n   Verificação: 1 vídeo, 1 faixa de áudio, 0 legendas.")
    else:
        info2 = probe(f2)
        before, after = count_types(info2), count_types(info_final)
        print("\n   Verificação (ficheiro 2 original -> final):")
        for key, label in (("video", "Vídeo"), ("audio", "Áudio"), ("subtitle", "Legendas"), ("attachment", "Anexos/fontes")):
            b, a = before.get(key, 0), after.get(key, 0)
            expected = b + 1 if key == "audio" else b
            flag = "OK" if a == expected else "ATENÇÃO"
            print(f"     {label}: {b} -> {a}  [{flag}]")
    if restore or modular_mode or opening_loop_mode:
        print(f"     Duração do corpo: {fmt_time(float(info_final['format']['duration']))}"
              + (" (+ abertura externa ligada)" if modular_mode else
                 " (abertura + corpo + abertura)" if opening_loop_mode else ""))
    print(f"   Resultado: {final}")
    return final


def cmd_workflow(args):
    if bool(args.op1) != bool(args.op2):
        sys.exit("Erro: tens de indicar ambos --op1 e --op2 para usar o modo modular de aberturas.")
    if args.opening and (args.op1 or args.op2):
        sys.exit("Erro: --opening não pode ser combinado com --op1/--op2.")
    if args.post_sync_start_cut < 0:
        sys.exit("Erro: --post-sync-start-cut não pode ser negativo.")
    if args.post_sync_start_cut and not args.auto_sync:
        sys.exit("Erro: --post-sync-start-cut requer --auto-sync.")
    if args.post_sync_start_cut and not args.no_restore:
        sys.exit("Erro: --post-sync-start-cut requer --no-restore.")
    if args.start_episode < 1:
        sys.exit("Erro: --start-episode tem de ser pelo menos 1.")
    if args.limit_episodes is not None and args.limit_episodes < 1:
        sys.exit("Erro: --limit-episodes tem de ser pelo menos 1.")

    p1 = Path(args.file1) if args.file1 else pick_file("Ficheiro 1 - de onde vem o ÁUDIO")
    p2 = Path(args.file2) if args.file2 else pick_file("Ficheiro 2 - o VÍDEO a cortar")

    if p1.is_dir() != p2.is_dir():
        sys.exit("Erro: ou indicas dois ficheiros, ou duas pastas.")
    if not p1.is_dir() and (args.start_episode != 1 or args.limit_episodes is not None):
        sys.exit("Erro: --start-episode e --limit-episodes só podem ser usados com duas pastas.")

    if args.audio_only:
        if args.auto_sync:
            sys.exit("Erro: --audio-only já está sincronizado; não pode ser usado com --auto-sync.")
        if not args.opening:
            sys.exit("Erro: --audio-only requer --opening para montar a abertura no início e no fim.")
        if p1.is_dir() or p2.is_dir():
            sys.exit("Erro: --audio-only requer um ficheiro de áudio e um ficheiro de vídeo, não pastas.")
        source_streams = probe(p1)["streams"]
        if any(s.get("codec_type") == "video" for s in source_streams):
            sys.exit("Erro: --audio-only requer um ficheiro sem vídeo.")
        if not audio_streams({"streams": source_streams}):
            sys.exit("Erro: o ficheiro indicado com --audio-only não tem faixa de áudio.")
        explicit_time_bounds = args.start_cut is not None and args.end_cut is not None
        explicit_chapter_bounds = args.start_chapter is not None and args.end_chapter is not None
        if not (explicit_time_bounds or explicit_chapter_bounds):
            sys.exit("Erro: --audio-only requer --start-cut e --end-cut, ou ambos --start-chapter e --end-chapter.")

    ask_cuts(args)

    if args.opening:
        opening_path = Path(args.opening)
        if not opening_path.is_file() or opening_path.suffix.lower() != ".mkv":
            sys.exit("Erro: --opening tem de ser um ficheiro MKV existente.")
        if not any(s.get("codec_type") == "audio" for s in probe(opening_path)["streams"]):
            sys.exit("Erro: o ficheiro --opening tem de conter uma faixa de áudio.")

    op_meta = None
    if args.op1 and args.op2:
        op1_path = Path(args.op1)
        op2_path = Path(args.op2)
        if not op1_path.is_file() or not op2_path.is_file():
            sys.exit("Erro: --op1 e --op2 têm de ser ficheiros .mkv válidos existentes.")
        print("\n[Preparação] A analisar as aberturas externas (--op1 e --op2)...")
        op_meta = {
            "op1_uid": get_segment_uid(op1_path),
            "op1_dur": float(probe(op1_path)["format"]["duration"]),
            "op2_uid": get_segment_uid(op2_path),
            "op2_dur": float(probe(op2_path)["format"]["duration"]),
        }
        print(f"   OP 1 ({op1_path.name}): UID={op_meta['op1_uid']} | Duração={fmt_time(op_meta['op1_dur'])}")
        print(f"   OP 2 ({op2_path.name}): UID={op_meta['op2_uid']} | Duração={fmt_time(op_meta['op2_dur'])}")

    if p1.is_dir():
        l1 = sorted(
            collect(p1, (".mkv",), args.recursive),
            key=lambda path: (path.name.casefold(), str(path).casefold()),
        )
        l2 = sorted(
            collect(p2, (".mkv",), args.recursive),
            key=lambda path: (path.name.casefold(), str(path).casefold()),
        )
        if len(l1) != len(l2):
            sys.exit(f"Erro: {len(l1)} ficheiros na pasta 1 vs {len(l2)} na pasta 2.")
        pairs = list(zip(l1, l2))
        if not pairs:
            sys.exit("Erro: não encontrei ficheiros .mkv nas pastas indicadas.")
        if args.start_episode > len(pairs):
            sys.exit(f"Erro: --start-episode {args.start_episode} ultrapassa os {len(pairs)} episódios encontrados.")
        pairs = pairs[args.start_episode - 1:]
        if args.limit_episodes is not None:
            pairs = pairs[:args.limit_episodes]
        if not pairs:
            sys.exit("Erro: não há episódios para processar com os limites indicados.")
        if args.start_episode != 1 or args.limit_episodes is not None:
            print(f"A processar episódios {args.start_episode}–{args.start_episode + len(pairs) - 1} "
                  f"de {len(l1)}, por ordem alfabética.")
        print("Pares (ficheiro 1 -> ficheiro 2):")
        for a, b in pairs:
            print(f"  {a.name}  ->  {b.name}")
        if not args.yes and input("\nOs pares estão corretos? [s/N] ").strip().lower() not in ("s", "sim", "y"):
            sys.exit("Cancelado.")
    else:
        pairs = [(p1, p2)]

    results = []
    is_bulk = p1.is_dir()
    batch_size = 10 if is_bulk else 1
    for batch_start in range(0, len(pairs), batch_size):
        batch = pairs[batch_start:batch_start + batch_size]
        sync_offset = None
        if is_bulk and args.auto_sync:
            first_audio, first_video = batch[0]
            print(f"\n[Sincronização do lote] a detetar usando {first_video.name}...")
            try:
                sync_offset = detect_sync(
                    first_audio,
                    first_video,
                    args.threshold,
                    args.detect_length,
                    args.detect_search,
                )["offset"]
            except Exception as e:
                print(f"   ERRO ao detetar a sincronização do lote: {e}")
                for index, (_, video) in enumerate(batch):
                    message = str(e) if index == 0 else (
                        f"lote ignorado porque a deteção no primeiro episódio falhou: {e}"
                    )
                    results.append((video.name, False, message))
                continue

        for a, b in batch:
            print(f"\n>> {a.name}  +  {b.name}")
            try:
                run_pair(a, b, args, op_meta, sync_offset=sync_offset)
                results.append((b.name, True, ""))
            except Exception as e:
                print(f"   ERRO: {e}")
                results.append((b.name, False, str(e)))
    report(results)


# ------------------------------------------------------------------------ CLI
def add_cut_options(sp):
    sp.add_argument("--start-cut", help="tempo a REMOVER do início (ex: 1:49)")
    sp.add_argument("--end-cut",
                    help="tempo adicional a remover após o fim do áudio (workflow com --no-restore/modular; ex: 1:30)")
    sp.add_argument("--start-chapter", type=int, help="começar no início deste capítulo (1 = primeiro)")
    sp.add_argument("--end-chapter", type=int, help="acabar no fim deste capítulo")
    sp.add_argument("--precise", action="store_true", help="corte ao frame (recodifica o vídeo; mais lento)")
    sp.add_argument("--crf", type=int, default=14, help="qualidade x264 em --precise (menor = melhor)")
    sp.add_argument("--ten-bit", action="store_true", help="usar 10-bit em --precise")


def add_detect_options(sp):
    sp.add_argument("--threshold", type=float, default=0.3,
                    help="sensibilidade da deteção de cortes de plano (menor = mais cortes; defeito 0.3)")
    sp.add_argument("--detect-length", type=float, default=240.0,
                    help="segundos do ficheiro 1 a analisar (defeito 240)")
    sp.add_argument("--detect-search", type=float, default=600.0,
                    help="margem extra no ficheiro 2 onde procurar (duração máx. da abertura; defeito 600)")


def build_parser():
    p = argparse.ArgumentParser(description="Ferramenta MKV: extrair áudio, cortar, adicionar faixa de áudio.")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp, positional=True):
        if positional:
            sp.add_argument("input", help="ficheiro .mkv OU pasta com ficheiros .mkv (bulk)")
        sp.add_argument("--out", help="pasta de destino (por defeito, junto ao original)")
        sp.add_argument("-r", "--recursive", action="store_true", help="procurar em subpastas")
        sp.add_argument("--overwrite", action="store_true", help="substituir ficheiros existentes")

    # extract
    e = sub.add_parser("extract", help="extrair áudio sem perdas")
    common(e)
    e.add_argument("--track", type=int, default=0, help="índice da faixa de áudio (0 = primeira)")
    e.add_argument("--lang", help="escolher faixa pelo idioma (ex: por, eng, jpn)")
    e.add_argument("--container", choices=["native", "mka"], default="native",
                   help="native = extensão do codec (.ac3, .flac...), mka = sempre Matroska Audio")
    e.set_defaults(func=cmd_extract)

    # trim
    t = sub.add_parser("trim", help="cortar início/fim")
    common(t)
    add_cut_options(t)
    t.add_argument("--suffix", default="_cut", help="sufixo do nome de saída (por defeito: _cut)")
    t.set_defaults(func=cmd_trim)

    # add-audio
    a = sub.add_parser("add-audio", help="adicionar ficheiro de áudio como nova faixa")
    common(a)
    a.add_argument("--audio", required=True,
                   help="ficheiro de áudio OU pasta (emparelha por nome: filme.mkv <-> filme.ac3)")
    a.add_argument("--lang", help="idioma da nova faixa (código ISO, ex: por)")
    a.add_argument("--title", help="nome da nova faixa (ex: 'Português')")
    a.add_argument("--default", action="store_true", help="tornar a nova faixa a predefinida")
    a.add_argument("--offset", type=float, default=0.0,
                   help="atraso do áudio em segundos (negativo = adiantar)")
    a.add_argument("--suffix", default="_audio", help="sufixo do nome de saída (por defeito: _audio)")
    a.set_defaults(func=cmd_add_audio)

    # workflow
    w = sub.add_parser("workflow", help="fluxo único: áudio do ficheiro 1 + vídeo cortado do ficheiro 2")
    w.add_argument("file1", nargs="?", help="ficheiro (ou pasta) de onde vem o áudio")
    w.add_argument("file2", nargs="?", help="ficheiro (ou pasta) do vídeo a cortar")
    common(w, positional=False)
    add_cut_options(w)
    w.add_argument("--src-track", type=int, default=0, help="faixa de áudio a extrair do ficheiro 1 (0 = primeira)")
    w.add_argument("--src-lang", help="escolher a faixa do ficheiro 1 pelo idioma (ex: por)")
    w.add_argument("--container", choices=["native", "mka"], default="native",
                   help="formato intermédio do áudio (native ou mka)")
    w.add_argument("--lang", help="idioma da nova faixa (por defeito, o da faixa original)")
    w.add_argument("--title", help="nome da nova faixa (por defeito, o da faixa original)")
    w.add_argument("--default", action="store_true", help="tornar a nova faixa a predefinida")
    w.add_argument("--offset", type=float, default=0.0,
                   help="atraso do áudio em segundos (negativo = adiantar)")
    w.add_argument("--suffix", default="_final", help="sufixo do nome de saída (por defeito: _final)")
    w.add_argument("--auto-sync", action="store_true",
                   help="detetar sozinho onde o áudio encaixa (compara os cortes de plano dos 2 vídeos)")
    w.add_argument("--post-sync-start-cut", type=float, default=0.0,
                   help="cortar tempo extra do início após o auto-sync e remover o mesmo do áudio (requer --no-restore)")
    w.add_argument("--start-episode", type=int, default=1,
                   help="episódio de início (posição 1-based na ordem alfabética; só com duas pastas)")
    w.add_argument("--limit-episodes", type=int,
                   help="número máximo de episódios a processar a partir de --start-episode")
    add_detect_options(w)
    w.add_argument("--no-restore", action="store_true",
                   help="NÃO repor o início/fim removidos: o resultado fica só com a secção central")
    w.add_argument("--op1", help="ficheiro MKV da abertura original (ativa modo modular Segment Linking)")
    w.add_argument("--op2", help="ficheiro MKV da abertura alternativa (ativa modo modular Segment Linking)")
    w.add_argument("--opening",
                   help="montar este MKV no início e no fim; combina o seu áudio com o áudio do ficheiro 1, remove legendas/faixas extra e recodifica para unir os segmentos")
    w.add_argument("--audio-only", action="store_true",
                   help="ficheiro 1 só tem áudio e já está sincronizado com o ficheiro 2; corta ambos pelos limites manuais (requer --opening)")
    w.add_argument("--op1-title", default="Abertura Original", help="nome da Edição 1 no leitor")
    w.add_argument("--op2-title", default="Abertura Alternativa", help="nome da Edição 2 no leitor")
    w.add_argument("--keep-temp", action="store_true", help="manter áudio e vídeo cortado intermédios")
    w.add_argument("-y", "--yes", action="store_true", help="não pedir confirmação dos pares (bulk)")
    w.set_defaults(func=cmd_workflow)

    # detect-sync
    d = sub.add_parser("detect-sync", help="só deteta onde o ficheiro 1 encaixa no ficheiro 2")
    d.add_argument("file1", help="ficheiro de onde vem o áudio")
    d.add_argument("file2", help="ficheiro de referência (com a abertura)")
    add_detect_options(d)
    d.set_defaults(func=cmd_detect_sync)

    return p


if __name__ == "__main__":
    args = build_parser().parse_args()
    req_mkvtoolnix = getattr(args, "op1", None) is not None or getattr(args, "op2", None) is not None
    check_tools(require_mkvtoolnix=req_mkvtoolnix)
    args.func(args)
