"""Tests for `pulp migrate` - moving existing items into the new subfolder layout."""

from __future__ import annotations

from pathlib import Path

from pulpline import pipeline
from pulpline.config import Config, Paths, Subscription, save_config
from pulpline.state import ItemRecord, connect, record_item


def _seed_item(
    conn: object,
    *,
    dedup: str,
    subscription_name: str | None,
    path: Path,
) -> None:
    record_item(
        conn,  # type: ignore[arg-type]
        ItemRecord(
            subscription_name=subscription_name,
            source_url="https://x/feed",
            dedup_key=dedup,
            canonical_url="https://x/post",
            title="t",
            pub_date=None,
            output_path=str(path),
        ),
    )


def test_migrate_moves_oneshots_to_oneshots_subfolder(tmp_path: Path) -> None:
    output_root = tmp_path / "Sync"
    output_root.mkdir()
    article = output_root / "Old Flat File.epub"
    article.write_bytes(b"epub")

    cfg = Config(paths=Paths(output_dir=str(output_root)))
    save_config(cfg)

    with connect() as conn:
        _seed_item(conn, dedup="k1", subscription_name=None, path=article)

    report = pipeline.migrate()

    new_path = output_root / "oneshots" / "Old Flat File.epub"
    assert new_path.exists()
    assert not article.exists()
    assert report.moved == 1


def test_migrate_moves_subscription_items_to_named_subfolder(tmp_path: Path) -> None:
    output_root = tmp_path / "Sync"
    output_root.mkdir()
    article = output_root / "Some Article.epub"
    article.write_bytes(b"epub")

    cfg = Config(
        paths=Paths(output_dir=str(output_root)),
        subscriptions=(Subscription(name="samkriss", source="rss", url="https://x/feed"),),
    )
    save_config(cfg)

    with connect() as conn:
        _seed_item(conn, dedup="k1", subscription_name="samkriss", path=article)

    pipeline.migrate()

    new_path = output_root / "samkriss" / "Some Article.epub"
    assert new_path.exists()
    assert not article.exists()


def test_migrate_dry_run_does_not_touch_disk(tmp_path: Path) -> None:
    output_root = tmp_path / "Sync"
    output_root.mkdir()
    article = output_root / "Article.epub"
    article.write_bytes(b"epub")

    cfg = Config(paths=Paths(output_dir=str(output_root)))
    save_config(cfg)
    with connect() as conn:
        _seed_item(conn, dedup="k1", subscription_name=None, path=article)

    report = pipeline.migrate(dry_run=True)

    assert article.exists()
    assert not (output_root / "oneshots" / "Article.epub").exists()
    assert report.moved == 1


def test_migrate_skips_already_correct(tmp_path: Path) -> None:
    output_root = tmp_path / "Sync"
    sub_dir = output_root / "samkriss"
    sub_dir.mkdir(parents=True)
    article = sub_dir / "Already.epub"
    article.write_bytes(b"epub")

    cfg = Config(
        paths=Paths(output_dir=str(output_root)),
        subscriptions=(Subscription(name="samkriss", source="rss", url="x"),),
    )
    save_config(cfg)
    with connect() as conn:
        _seed_item(conn, dedup="k1", subscription_name="samkriss", path=article)

    report = pipeline.migrate()
    assert report.moved == 0
    assert report.skipped_already_correct == 1
    assert article.exists()


def test_migrate_skips_collision(tmp_path: Path) -> None:
    output_root = tmp_path / "Sync"
    output_root.mkdir()
    article = output_root / "x.epub"
    article.write_bytes(b"v1")
    target_dir = output_root / "oneshots"
    target_dir.mkdir()
    (target_dir / "x.epub").write_bytes(b"v2-already-there")

    cfg = Config(paths=Paths(output_dir=str(output_root)))
    save_config(cfg)
    with connect() as conn:
        _seed_item(conn, dedup="k1", subscription_name=None, path=article)

    report = pipeline.migrate()
    assert report.skipped_collision == 1
    # Original is untouched, target is untouched.
    assert article.read_bytes() == b"v1"
    assert (target_dir / "x.epub").read_bytes() == b"v2-already-there"


def test_migrate_orphaned_subscription_left_alone(tmp_path: Path) -> None:
    output_root = tmp_path / "Sync"
    output_root.mkdir()
    article = output_root / "ghost.epub"
    article.write_bytes(b"x")

    # Config has no subscription named "ghost-sub".
    cfg = Config(paths=Paths(output_dir=str(output_root)))
    save_config(cfg)
    with connect() as conn:
        _seed_item(conn, dedup="k1", subscription_name="ghost-sub", path=article)

    report = pipeline.migrate()
    assert report.orphaned == 1
    assert article.exists()


def test_migrate_missing_file_counted(tmp_path: Path) -> None:
    cfg = Config(paths=Paths(output_dir=str(tmp_path / "Sync")))
    save_config(cfg)
    with connect() as conn:
        _seed_item(
            conn,
            dedup="k1",
            subscription_name=None,
            path=tmp_path / "never-existed.epub",
        )

    report = pipeline.migrate()
    assert report.missing_on_disk == 1
    assert report.moved == 0
