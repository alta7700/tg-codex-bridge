# Telegram ↔ Codex Desktop bridge

Локальный мост для одного Telegram-чата, существующего чата Codex Desktop и
одного workspace. Бот принимает сообщения/вложения; агент отвечает через CLI или отмечает
сообщение как не требующее ответа через tg-ignore. Исходники находятся в этом каталоге; рабочие данные хранятся отдельно от Git.

## Установка и привязка

Нужны macOS, установленный Codex Desktop и Python 3.11+; рабочее окружение
проверялось с Python 3.13. Зависимости зафиксированы в uv.lock.

```sh
cd telegram-bridge
UV_CACHE_DIR=../.runtime/cache/uv uv sync --group dev --python 3.13
cp .env.example .env
```

В локальной .env укажи TG_CHAT_ID, CODEX_THREAD_ID существующего Desktop-чата,
WORKSPACE и при необходимости CODEX_BIN. WORKSPACE=.. использует родительский
каталог. Явная .env важнее унаследованного CODEX_THREAD_ID другого чата.
TG_USER_IDS необязателен: пустой список разрешает всех людей указанной группы.
Сообщения ботов игнорируются. Анонимные сообщения от имени группы принимаются,
если TG_USER_IDS пуст; личность владельца они не подтверждают. На сообщение человека из другого чата
сам мост отвечает `чат {id} не разрешен`, без вызова Codex и сохранения его текста
в inbox/history. Ответ служебный и best-effort: Telegram-отказ/сеть не блокируют
приём разрешённого чата. User allowlist внутри разрешённого чата остаётся отдельно.

Токен положи в private/bot-token: каталог 700, файл 600, владелец — текущий user.
В CLI и model environment токен не передаётся. В Telegram группе отключи Group
Privacy бота, если нужны обычные сообщения; webhook должен отсутствовать.
Мост не удаляет webhook автоматически. Настройки и token не коммитятся.

Сообщения с превью ссылки, включая reply на такое сообщение, сохраняются через
штатный сериализатор aiogram. Не заменяйте его обычным model_dump_json:
SDK-defaults в link_preview_options могут остановить приём очереди.

Для подтверждения ограниченных действий из AGENTS.md локально создай
private/operator.json с числовыми telegram_user_id и telegram_chat_id владельца.
Дополнительно можно хранить display_name/telegram_username; они не заменяют ID.
Этот файл не является токеном, агент может прочесть его для проверки владельца;
он тоже исключён из Git. Для обычных ответов участникам этот реестр не обязателен.

## Запуск

Дождись завершения активных задач Desktop, закрой приложение через Cmd+Q,
затем вручную запусти:

```sh
./start-shared-desktop.command
```

Команда открывает Desktop и освобождает терминал. Не запускай её при работающем
приложении. Для возврата к обычному запуску закрой Desktop и открой из Dock.
Пока мост работает, Desktop должен оставаться открыт.

```sh
.venv/bin/tg-bridge doctor
./manage.sh start
./manage.sh status
./manage.sh restart
./manage.sh stop
```

manage.sh запускает Telegram-процесс в фоне, пишет .state/bridge.log и управляет
только процессом, удерживающим lock этого workspace: проверяет UID/команду/cwd,
останавливает через SIGTERM, сохраняет очередь и отказывается трогать неизвестный
PID. Отдельный manage.lock сериализует команды управления. Не используется sudo,
SIGKILL, launchd или перезапуск Desktop. Для foreground-запуска есть ./run.sh.
Не запускай второй poller того же бота. CLI управления может требовать обычное
согласование исполнения в Desktop, если sandbox блокирует локальный IPC/процессы.

При Telegram queue ready бот принимает сообщения и скачивает файлы; Codex
подключается при доставке. doctor отдельно показывает codex_app и наличие
send_message_to_thread: одного успешного RPC-соединения недостаточно для проверки
всех инструментов. Новые чаты при запуске не создаются.

## Очередь, lifecycle и порядок

`queued → downloading → ready → processing → processed / failed`

- Приём сохраняет SQLite inbox до подтверждения update offset. Ключ
  (chat_id,message_id) предотвращает повторное создание события.
- Вложения скачиваются FIFO в workspace/tg_input/<message_id>/; имя файла тоже
  содержит message_id. Частичные .part публикуются через atomic rename.
  Пока раннее вложение грузится, последующие входы не доставляются агенту.
- После доставки вход становится processing; следующий вход не ждёт ответа.
  Несколько сообщений могут обрабатываться в одном turn.
- Подтверждённый Telegram reply либо явный tg-ignore переводят вход в processed.
  Раннее «нужно время» тоже считается ответом, а не окончанием исследования.
  resolution=replied/ignored различает способы обработки; ignore не создаёт
  исходящего сообщения и не заполняет replied_at. Сообщение без reply и завершение
  Codex turn сами по себе не закрывают вход.
- Через 5 минут без reply/ignore один раз доставляется «напоминаю, если забыл»;
  ещё через 5 минут вход становится failed. Общий turn не прерывается.
  Таймеры durable; поздний reply или ignore меняет failed на processed.
- Мост сам ставит 👀, 👍 и 👎. Ошибка реакции не повторяет отправку ответа.
  Первая подтверждённая часть длинного reply уже считается ответом.

Bridge сериализует доставку и reminders. Перед выбором start/steer читает свежий
thread/turns/list (limit=1, sortDirection=desc, itemsView=notLoaded). Для активного
turn использует turn/steer с expectedTurnId, иначе turn/start; при явной гонке
перечитывает состояние и использует фактический turn.id из ACK. Порядок между
Desktop и Telegram глобально не гарантируется: общей транзакции у двух интерфейсов
нет. Потерянный ACK сверяется с историей по стабильному telegram-event/reminder
маркеру; потенциально принятый input не повторяется вслепую. Неопределённость
исходной доставки блокирует следующие входы до reconciliation/решения владельца.

Транспортные инструкции добавляются при resume. Мост не переопределяет профиль,
approval policy, cwd или runtime workspace roots чата. Подробности протокола:
[Codex App Server](https://learn.chatgpt.com/docs/app-server).

## Функции агента

Полные CLI-пути, CHAT_ID и SOCKET сообщаются агенту транспортом. SOCKET можно
узнать через tg-bridge status. CLI требует --socket, а не читает .env/токен.

```sh
.venv/bin/tg-send --socket SOCKET --chat-id CHAT_ID \
  --reply-to MESSAGE_ID --request-id UNIQUE_ID --text 'Нужно время: проверяю'
.venv/bin/tg-send-file --socket SOCKET --chat-id CHAT_ID \
  --reply-to MESSAGE_ID --file output/telegram/report.txt
.venv/bin/tg-ignore --socket SOCKET --chat-id CHAT_ID \
  --message-id MESSAGE_ID --reason 'переписка между участниками'
.venv/bin/tg-history --socket SOCKET --chat-id CHAT_ID --limit 50
.venv/bin/tg-history --socket SOCKET --chat-id CHAT_ID --message-id MESSAGE_ID
```

Без --reply-to возможна отдельная публикация в привязанную группу, но она не
закрывает вход. Файлы передаются относительными путями от workspace, без .. .
Проверяются обычный файл, symlink/hardlink, private/hidden paths и выход за workspace;
передача использует закреплённый file descriptor. Производные отчёты удобно класть
в output/telegram; это не ограничение записи кода/других файлов workspace.

tg-ignore применяется только к известному входу разрешённого чата; команда
идемпотентна по message_id, очищает таймер ожидания и ставит 👍 через мост. Причина
до 500 символов и ignored_at сохраняются локально. Повтор не меняет первое решение;
уже подтверждённый reply не превращается в ignored. После ignore можно ответить
позже: resolution станет replied, сохранив сведения о прежнем ignore.

История содержит принятые входы и подтверждённые исходящие тексты/файлы, ID,
авторов (user_id, имя, username/mention), reply context, статусы и resolution.
Для игнорирования доступны ignored_at/ignore_reason, для ответа — replied_at. Поддерживаются
--before-message-id, --after-message-id, --status; JSON возвращает next_cursor и
cursor_direction. Произвольная старая переписка Telegram не загружается.
В событие reply сразу включены автор и текст/подпись оригинала, один уровень;
при отсутствии snapshot используется локальная история. Имена не заменяют ID.

Подтверждённая отправка, история и lifecycle коммитятся одной SQLite-транзакцией.
Повтор request_id с тем же payload идемпотентен для подтверждённых отправок;
иной payload с тем же ID отклоняется. Telegram не даёт общей транзакции с SQLite:
сетевой сбой после send может оставить uncertain, без автоматического replay.
Fallback reply в самостоятельное сообщение запрещён: ошибка reply не закрывает вход.

Sandbox может блокировать Unix socket: CLI возвращает ipc_permission_denied.
Агент запрашивает обычное согласование конкретной CLI-команды через
exec_command sandbox_permissions=require_escalated, без sudo и без обхода отказа.
delivery=not_submitted означает отказ до передачи; delivery=unknown после send/ACK
требует сохранения request_id и проверки результата. Правила разрешений описаны
в [официальной документации](https://learn.chatgpt.com/docs/permissions).

## Адаптер Desktop

Bootstrap desktop-codex делает exec штатного подписанного CLI, сохраняя его PID,
родителя Desktop, исходные параметры и stdio. Дочерний Python-брокер multiplexes
JSONL между Desktop и CLI, предоставляя приватный Unix WebSocket мосту. Брокер
разделяет ID запросов, пересылает notifications, направляет approvals/auth/attestation
настоящему Desktop. Genuine upstream initialize единственный; подтверждённого ACK
достаточно — отдельный initialized от этой версии Desktop не требуется.

Цепочка MCP Node → CLI → Desktop сохраняется: Python между CLI и Desktop нарушал
проверку подписей с missing-code-signing-identity. Проверка не отключается,
приложение не патчится. Startup probes (включая app-server --print-startup-config),
help и вспомогательные stdio-серверы передаются без изменения stdout/exit status.
Брокер обрабатывает только основной наблюдаемый запуск с analytics-default-enabled.
При EOF Desktop закрывается stdin CLI; выход CLI закрывает брокер и сокет.

## Восстановление и смена чата

Останови мост перед локальной коррекцией. Проверь историю Codex/Telegram перед retry.

```sh
.venv/bin/tg-bridge status
.venv/bin/tg-bridge resolve SEQ retry
.venv/bin/tg-bridge resolve SEQ skip
.venv/bin/tg-bridge resolve-send REQUEST_ID retry
.venv/bin/tg-bridge resolve-send REQUEST_ID skip
.venv/bin/tg-bridge bind EXISTING_THREAD_ID
```

bind требует разрешённую/завершённую очередь. Raw history Codex не редактируется;
SQLite мигрируется добавлением полей. При снятии прежнего sticky override в уже
загруженном чате используется thread/settings/update: thread/resume может не
применить изменение. Мост сам новые permission profiles не устанавливает.

## Проверки и ограничения

```sh
.venv/bin/pytest -q
```

Последний полный прогон: 78 тестов прошли.

Проверены FIFO с загрузкой/повторами, lifecycle, обе turn-гонки, виртуальные таймеры
5+5, ACK loss/restart, IPC и CLI ошибки, reply/history/reactions, границы файлов,
PID/parent и native request routing Desktop-брокера. Менеджер жизненного цикла
проверен на start/stop/restart с сохранением данных и отказ
трогать неизвестный lock-holder. Ответ чужому чату проверяется с обычной отправкой,
Telegram Forbidden и сетевой ошибкой, включая сохранение приёма разрешённого чата.
Отдельная проверка на настоящем
CLI подтвердила stdio/WebSocket, read-only RPC, сохранение ancestry и завершение,
включая owner initialize без initialized. Живой пользовательский тест подтвердил
reply, историю, реакции и поздний failed → processed. Автотесты в группу не пишут.

Пределы: download 20 MiB, upload 49 MiB; длинный текст делится по UTF-16, 429
учитывает retry_after. Альбомы представлены отдельными входами с media_group_id;
редактирование/удаление не синхронизируется. Во время сна ноутбука обработка не идёт;
Telegram хранит неполученные updates ограниченное время. Локальная гарантия начинается
после durable приёма. Логи не содержат token/тексты; SQLite/tg_input приватны.

Основные инструкции — в [AGENTS.md](../AGENTS.md). Самостоятельные улучшения,
лимиты ресурсов и согласования описаны в проектном skill
[improve-telegram-bridge](../.agents/skills/improve-telegram-bridge/SKILL.md);
передача следующему чату — в [HANDOFF.md](HANDOFF.md).
Локальные ID и прежние диагностические записи не публикуются в репозитории.
