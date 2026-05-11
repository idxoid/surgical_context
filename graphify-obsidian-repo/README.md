# graphify-obsidian-repo

Bundled tooling: **Cursor agent transcripts → Obsidian**, plus sibling scripts for Codex/Claude (см. `scripts/`).

Никакой отдельной ветки формата под «модель» в Cursor не нужен: все агентные диалоги пишутся в `~/.cursor/projects/*/agent-transcripts/**/*.jsonl` с полями `role` и `message.content` (список блоков `text` / `tool_use`). Экспортёр читает **все** такие файлы.

---

## Cursor → Obsidian

### Переменные окружения

| Переменная | Назначение |
|------------|------------|
| **`VAULT_DIR`** | Корень Obsidian vault (по умолчанию `$HOME/vault`). |
| **`OBSIDIAN_VAULT_PROJECT`** | Подпапка внутри vault: куда писать заметки и откуда брать существующие `.md` для wikilinks. По умолчанию в скрипте Python: **`surgical_context`**. Для vault как в `~/vault/dathund/` задай **`export OBSIDIAN_VAULT_PROJECT=dathund`**. |
| **`CURSOR_PROJECT_SUBSTRING`** | Если задано — обрабатываются только проекты Cursor, чей slug содержит эту подстроку (например `home-idxoid-dathund`). Иначе экспортируются **все** workspace’ы. |
| **`LOG`** | Лог синка (по умолчанию `$HOME/scripts/sync_cursor.log`). |

### Одноразовый запуск

```bash
export OBSIDIAN_VAULT_PROJECT=dathund   # или surgical_context
./scripts/sync_cursor_obsidian.sh
```

Или напрямую:

```bash
python3 scripts/cursor_agent_transcripts_to_obsidian.py \
  --vault-dir ~/vault \
  --vault-project dathund \
  --dry-run
```

Убери `--dry-run` для записи файлов в `~/vault/<project>/chats/cursor/`.

### Cron (ежедневно)

```bash
0 22 * * * OBSIDIAN_VAULT_PROJECT=dathund VAULT_DIR=$HOME/vault /path/to/graphify-obsidian-repo/scripts/sync_cursor_obsidian.sh
```

### Graphify по чатам

После экспорта можно собрать граф только по папке чатов:

```bash
graphify ~/vault/dathund/chats/cursor --update
```

---

## Публикация как отдельный git-репозиторий

1. `git subtree split --prefix=graphify-obsidian-repo -b graphify-obsidian-split` (из родительского монорепо, если каталог там лежит с префиксом).
2. Push в новый remote.
3. (Опционально) submodule обратно в основной проект.

---

## Файлы

- `scripts/cursor_agent_transcripts_to_obsidian.py` — экспорт JSONL → Markdown + frontmatter.
- `scripts/sync_cursor_obsidian.sh` — обёртка с логом.
- `scripts/sync_codex_obsidian.sh`, `scripts/sync_claude_obsidian.sh` — другие каналы.
- `setup.md` — длинное руководство (Claude-heavy); этот README — краткая шпаргалка для Cursor.
