# GitHub公開前チェックリスト

## 目的

このリポジトリを GitHub に上げる前に、機密情報、ローカル依存、壊れたリンクを落とさないための確認項目をまとめる。

## 1. コミット対象外の確認

- `var/` をコミットしない
- `config/*.local.toml` をコミットしない
- `__pycache__/`, `.pytest_cache/`, `build/`, `dist/`, `*.egg-info/` をコミットしない
- `git status` 相当で runtime artifact が混ざっていないことを確認する

## 2. 機密情報の確認

- token を含むファイルやログ断片を含めていない
- `gateway.auth.token` の実値を書いていない
- `Authorization` header や bearer token を残していない
- 実 SSH 秘密鍵や `authorized_keys` を含めていない
- 外部通知先 URL や webhook secret を含めていない

## 3. 環境依存値の確認

- 実 IP を公開してよいか確認する
- 実ホスト名、ユーザー名、LAN 名称を公開してよいか確認する
- 実機専用パスが README の主導線になっていないことを確認する
- 公開文書では `config/relay.example.toml` を主に参照し、実機 local config は説明だけに留める

## 4. ドキュメント確認

- [../README.md](../README.md) のリンクが GitHub 上で辿れる
- [文書一覧.md](文書一覧.md) から主要文書に遷移できる
- 内部リンクがローカル絶対パスではなく相対リンクになっている
- 会話 UI と監視 UI の違いが README で分かる
- setup、systemd、Prometheus、Alertmanager の導線が README にある

## 5. 最低限の動作確認

- `PYTHONPATH=src python3 -m unittest discover -s tests -v`
- `PYTHONPATH=src python3 -m openclaw_relay --config config/relay.example.toml check-config`
- 必要なら実機で `curl http://127.0.0.1:18080/healthz`
- 必要なら実機で `curl http://127.0.0.1:18080/readyz`

## 6. GitHub に含めるもの

- `src/`
- `tests/`
- `config/relay.example.toml`
- `config/relay.hosts.example`
- `deploy/systemd/`
- `deploy/monitoring/`
- `doc/`
- `README.md`
- `pyproject.toml`

## 7. 公開後の利用者向けに明確化すること

- 実運用には local config 作成が必要
- `OptionABC001` と worker の実機情報は利用者が自分で埋める
- `config/*.local.toml` はサンプルから生成する前提
- `systemd` と Prometheus は外部依存であり、リポジトリ内にはバイナリを含めない

## 8. 公開直前の最終確認

- README の冒頭だけ読んで、初見でも役割が分かる
- 文書一覧から迷わず必要文書へ行ける
- `.gitignore` が意図どおり効いている
- 公開したくない session transcript や audit 抜粋が残っていない
