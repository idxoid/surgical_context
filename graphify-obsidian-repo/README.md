# graphify-obsidian-repo

Набор обёрток: экспорт диалогов **Cursor**, **Claude Code** и **Codex** (расширение VS Code) → Obsidian, плюс извлечение «решений» из чатов Claude/Codex.

Общая идея: один каталог vault (`VAULT_DIR`) и одна подпапка проекта (`OBSIDIAN_VAULT_PROJECT`), например **`dathund`** — тогда заметки лежат в `~/vault/dathund/...`.

Форматы разных продуктов **не зависят от выбранной LLM внутри** клиента: у каждого канала свой источник файлов на диске (см. ниже).

---

## Общие переменные (все три канала)

| Переменная | Назначение |
|------------|------------|
| **`VAULT_DIR`** | Корень Obsidian vault (по умолчанию `$HOME/vault`). |
| **`OBSIDIAN_VAULT_PROJECT`** | Подпапка внутри vault (`dathund`, `surgical_context`, …). Совпадает с `--vault-project` / `--project` в Python. По умолчанию в скриптах: **`surgical_context`**. |
| **`OBSIDIAN_SCRIPT_DIR`** | Где лежат процессоры **`claude_to_obsidian.py`**, **`codex_to_obsidian.py`**, **`extract_decisions_*.py`** (по умолчанию **`$HOME/scripts`**). |
| **`LOG`** | Файл лога (у каждого sync-скрипта свой дефолт — см. разделы). |

---

## Cursor → Obsidian

Источник: `~/.cursor/projects/*/agent-transcripts/**/*.jsonl` (Composer / Agent).

| Доп. переменная | Назначение |
|-----------------|------------|
| **`CURSOR_PROJECT_SUBSTRING`** | Ограничить workspace по подстроке в slug (например `home-idxoid-dathund`). Без переменной обрабатываются все проекты. |
| **`LOG`** | По умолчанию `$HOME/scripts/sync_cursor.log`. |

### Одноразовый запуск

```bash
export OBSIDIAN_VAULT_PROJECT=dathund
./scripts/sync_cursor_obsidian.sh
```

Или напрямую:

```bash
python3 scripts/cursor_agent_transcripts_to_obsidian.py \
  --vault-dir ~/vault \
  --vault-project dathund \
  --dry-run
```

Вывод: `~/vault/<project>/chats/cursor/`.

### Cron

```bash
0 22 * * * OBSIDIAN_VAULT_PROJECT=dathund VAULT_DIR=$HOME/vault /path/to/graphify-obsidian-repo/scripts/sync_cursor_obsidian.sh
```

### Graphify

```bash
graphify ~/vault/dathund/chats/cursor --update
```

---

## Claude Code → Obsidian

Источник: экспорт **`claude-extract`** в `~/claude-exports/code` (и при необходимости web → `~/claude-exports/web`). Отдельные модели Opus/Sonnet не меняют формат экспорта — важен канал Claude Code, не имя модели.

| Доп. переменная | Назначение |
|-----------------|------------|
| **`CLAUDE_EXPORT_DIR`** | Куда складывает **`claude-extract`** (по умолчанию `$HOME/claude-exports`). |
| **`LOG`** | По умолчанию `$HOME/scripts/sync.log`. |

### Зависимости

- **`pip install claude-conversation-extractor`** (команда **`claude-extract`** на PATH).
- В **`OBSIDIAN_SCRIPT_DIR`**: **`claude_to_obsidian.py`**, **`extract_decisions_from_claude_code.py`** (типично копии из твоего `~/scripts`).

### Одноразовый запуск

```bash
export OBSIDIAN_VAULT_PROJECT=dathund
./scripts/sync_claude_obsidian.sh
```

Или без обёртки:

```bash
python3 ~/scripts/claude_to_obsidian.py \
  --export-dir ~/claude-exports \
  --vault-dir ~/vault \
  --project dathund \
  --move

python3 ~/scripts/extract_decisions_from_claude_code.py \
  --vault-dir ~/vault \
  --project dathund
```

Вывод чатов: `~/vault/<project>/chats/code/` (и `chats/web/` для web-экспорта). Решения: `~/vault/<project>/decisions/`.

### Cron

```bash
15 22 * * * OBSIDIAN_VAULT_PROJECT=dathund /path/to/graphify-obsidian-repo/scripts/sync_claude_obsidian.sh
```

### Graphify

```bash
graphify ~/vault/dathund/chats/code --update
```

---

## Codex → Obsidian

Источник: JSONL сессий расширения Codex в **`~/.codex/sessions/`** (или **`CODEX_DIR`**).

| Доп. переменная | Назначение |
|-----------------|------------|
| **`CODEX_DIR`** | Корень данных Codex (по умолчанию `$HOME/.codex`). |
| **`LOG`** | По умолчанию `$HOME/scripts/sync_codex.log`. |

### Зависимости

- В **`OBSIDIAN_SCRIPT_DIR`**: **`codex_to_obsidian.py`**, **`extract_decisions_from_codex.py`**.

### Одноразовый запуск

```bash
export OBSIDIAN_VAULT_PROJECT=dathund
./scripts/sync_codex_obsidian.sh
```

Или напрямую:

```bash
python3 ~/scripts/codex_to_obsidian.py \
  --codex-dir ~/.codex \
  --vault-dir ~/vault \
  --project dathund

python3 ~/scripts/extract_decisions_from_codex.py \
  --vault-dir ~/vault \
  --project dathund
```

Вывод чатов: `~/vault/<project>/chats/codex/`. Решения: `~/vault/<project>/decisions/` (общая папка с Claude-пайплайном; при желании разнеси вручную другим `--output-dir`).

### Cron

```bash
30 22 * * * OBSIDIAN_VAULT_PROJECT=dathund /path/to/graphify-obsidian-repo/scripts/sync_codex_obsidian.sh
```

### Graphify

```bash
graphify ~/vault/dathund/chats/codex --update
```

---

## Публикация как отдельный git-репозиторий

1. `git subtree split --prefix=graphify-obsidian-repo -b graphify-obsidian-split` (из родительского монорепо, если каталог лежит с префиксом).
2. Push в новый remote.
3. (Опционально) submodule обратно в основной проект.

---

## Файлы в этом каталоге

| Файл | Назначение |
|------|------------|
| `scripts/cursor_agent_transcripts_to_obsidian.py` | Cursor JSONL → Markdown. |
| `scripts/sync_cursor_obsidian.sh` | Обёртка + лог. |
| `scripts/sync_claude_obsidian.sh` | Claude extract + импорт + decisions. |
| `scripts/sync_codex_obsidian.sh` | Codex JSONL → vault + decisions. |
| `setup.md` | Длинное руководство (в т.ч. Zettelkasten / Graphify); этот README — короткая шпаргалка по трём каналам. |
