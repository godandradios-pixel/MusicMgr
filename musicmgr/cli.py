"""Headless command line for scanning, importing charts and inspecting the DB.

    python -m musicmgr.cli scan ~/Music
    python -m musicmgr.cli scan-videos ~/Videos/Music
    python -m musicmgr.cli import-chart hot100.csv --name "Billboard Hot 100"
    python -m musicmgr.cli artist-images ~/Pictures/Artists
    python -m musicmgr.cli stats
    python -m musicmgr.cli top --window month
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .db.session import init_engine, session_scope
from .services import charts as chart_svc
from .services import library as lib
from .services import playlists as pl_svc
from .services import scanner
from .services import video_scanner
from .services import videos as vid_svc


def cmd_scan(args) -> int:
    with session_scope() as session:
        result = scanner.ScanResult()
        for path in args.paths:
            scanner.scan_folder(
                session,
                path,
                progress=lambda d, t, n: print(f"\r  {d}/{t} {n[:60]:<60}", end=""),
                result=result,
            )
        result.missing = scanner.mark_missing_files(session)
        result.removed = scanner.purge_orphaned_tracks(session)
    print("\n" + result.summary())
    for err in result.errors[:10]:
        print("  !", err)
    return 0


def cmd_scan_videos(args) -> int:
    with session_scope() as session:
        result = video_scanner.ScanResult()
        for path in args.paths:
            video_scanner.scan_video_folder(
                session,
                path,
                progress=lambda d, t, n: print(f"\r  {d}/{t} {n[:60]:<60}", end=""),
                result=result,
            )
        result.missing = video_scanner.mark_missing_videos(session)
    print("\n" + result.summary())
    for err in result.errors[:10]:
        print("  !", err)
    return 0


def cmd_import_chart(args) -> int:
    with session_scope() as session:
        for path in args.paths:
            result = chart_svc.import_chart_csv(session, path, chart_name=args.name)
            print(result.summary())
            for err in result.errors[:10]:
                print("  !", err)
    return 0


def cmd_artist_images(args) -> int:
    from .services import artist_images as art_svc

    with session_scope() as session:
        result = art_svc.import_artist_images(
            session, args.folder, overwrite=not args.keep_existing
        )
        print(result.summary())
        for name, artist, score in result.fuzzy:
            print(f"  ~ {name} -> {artist} ({score:.0%})")
        for name in result.unmatched:
            print(f"  ? {name}")
        for err in result.errors:
            print(f"  ! {err}")
        missing = art_svc.artists_without_images(session, limit=args.show_missing)
        if missing:
            print(f"\nStill without a picture ({len(missing)} shown):")
            for name in missing:
                print(f"  {name}")
    return 0


def cmd_stats(_args) -> int:
    with session_scope() as session:
        stats = lib.library_stats(session)
        stats.update(vid_svc.video_counts(session))
    width = max(len(k) for k in stats)
    for key, value in stats.items():
        shown = lib.format_duration(value) if key.endswith("_ms") else f"{value:,}"
        print(f"{key:<{width}}  {shown}")
    return 0


def cmd_top(args) -> int:
    with session_scope() as session:
        rows = pl_svc.playback_top_tracks(session, window=args.window, limit=args.limit)
        for rank, (track, plays, ms) in enumerate(rows, start=1):
            print(
                f"{rank:>3}. {track.title[:44]:<44} "
                f"{(track.artist_display or '')[:28]:<28} {plays:>4} plays"
            )
    if not rows:
        print("No qualifying plays in that window yet.")
    return 0


def cmd_charts(_args) -> int:
    with session_scope() as session:
        for chart in chart_svc.list_charts(session):
            issues = chart_svc.list_issues(session, chart.id)
            print(f"[{chart.kind}] {chart.name} — {len(issues)} edition(s)")
            for issue in issues[:5]:
                owned, total = chart_svc.issue_coverage(session, issue.id)
                print(f"    {issue.chart_date}  {owned}/{total} in library")
    return 0


def cmd_snapshot(args) -> int:
    with session_scope() as session:
        playlist = chart_svc.snapshot_to_playlist(session, args.issue_id)
        print(f"Created playlist: {playlist.name} ({len(playlist.items)} tracks)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="musicmgr", description=__doc__)
    parser.add_argument("--db", help="path to an alternate library.db")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scan", help="scan folders for audio files")
    p.add_argument("paths", nargs="+")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("scan-videos", help="scan folders for video files")
    p.add_argument("paths", nargs="+")
    p.set_defaults(func=cmd_scan_videos)

    p = sub.add_parser("import-chart", help="import Billboard-style CSV")
    p.add_argument("paths", nargs="+")
    p.add_argument("--name", help="chart series name")
    p.set_defaults(func=cmd_import_chart)

    p = sub.add_parser("artist-images", help="import a folder of artist portraits")
    p.add_argument("folder")
    p.add_argument("--keep-existing", action="store_true",
                   help="do not replace portraits already set")
    p.add_argument("--show-missing", type=int, default=20)
    p.set_defaults(func=cmd_artist_images)

    p = sub.add_parser("stats", help="library counts")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("top", help="most-played tracks")
    p.add_argument("--window", default="week", choices=list(pl_svc.PLAYBACK_WINDOWS))
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_top)

    p = sub.add_parser("charts", help="list imported charts")
    p.set_defaults(func=cmd_charts)

    p = sub.add_parser("snapshot", help="freeze a chart edition as a playlist")
    p.add_argument("issue_id", type=int)
    p.set_defaults(func=cmd_snapshot)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    init_engine(Path(args.db) if args.db else None)
    with session_scope() as session:
        pl_svc.ensure_default_playlists(session)
        pl_svc.ensure_builtin_playback_playlists(session)
        chart_svc.remove_orphaned_playback_charts(session)
        lib.fix_artist_sort_keys(session)
        lib.backfill_album_artists(session)
        lib.merge_compilation_duplicates(session)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
