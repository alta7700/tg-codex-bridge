# Передача работы следующему чату

Сначала прочитай корневой AGENTS.md, затем TELEGRAM.md.
После каждого compact перечитай AGENTS.md, TELEGRAM.md и этот HANDOFF.md;
напоминание об этом сохраняй в контексте. В этом workspace уже есть
работающий мост; не пересоздавай окружение, бота или чат. История предыдущего
разработчика не требуется для работы. Локальная `.env` хранит привязку,
`private/operator.json` — проверяемую личность владельца; оба файла исключены из Git.
Токен читает только процесс бота из `private/bot-token`. Рабочие env не меняй,
не открывай для просмотра моделью и не раскрывай; helper bind тоже меняет .env
и требует прямого поручения владельца. В `.env.example` допустимы лишь шаблоны.

## Проверка и обычное управление

Из каталога `telegram-bridge`:

```sh
./manage.sh status
.venv/bin/tg-bridge doctor
.venv/bin/tg-bridge status
```

doctor проверяет App Server и наличие send_message_to_thread в codex_app.
status показывает durable inbox/outbox; successful turn сам по себе не означает
ответ в Telegram. Вход также можно осознанно закрыть без ответа через tg-ignore;
resolution в истории различает ignored и replied. Локальные ID и сокеты узнавай из результата doctor/status.
Не копируй их в Git или публичный issue. Полная локальная история прежней
диагностики, если понадобится, сохранена в `.state/README-local-diagnostics-before-git.md`.

```sh
./manage.sh start
./manage.sh restart
./manage.sh stop
```

manage.sh управляет только процессом `tg-bridge run`, который удерживает lock-файл
этого workspace, проверяя UID/команду/cwd. Используется SIGTERM, без SIGKILL,
перезапуска Desktop или удаления очереди. Новый процесс запускается detached;
логи — `.state/bridge.log`. Неизвестный держатель lock не завершается.
При необходимости запроси штатное согласование исполнения команды в Desktop.

Для самого Desktop остаётся ручной запуск start-shared-desktop.command после
завершения других задач и Cmd+Q. Не запускай эту команду при работающем Desktop.

## Разработка

При исправлении моста, добавлении функции или установке инструмента используй
проектный skill [$improve-telegram-bridge](../.agents/skills/improve-telegram-bridge/SKILL.md).
В нём находятся самостоятельный цикл изменений, лимиты ресурсов, проверка
владельца, тесты, коммиты и безопасное применение. Не устанавливай TTS или другую
модель лишь из-за примера в инструкциях: нужен конкретный пользовательский запрос.

## Что уже проверено

- Полный цикл: Telegram input → Codex → reply или tg-ignore → processed → 👍.
  История отличает replied/ignored; ignore не создаёт исходящий текст.
- tg-history, вложения/reply context, FIFO до доставки, start/steer и гонки.
- Чужому чату отвечает сам мост: «чат {id} не разрешен»; Codex не вызывается.
- Вход от имени группы/канала (sender_chat) принимается в разрешённом чате,
  если нет личного TG_USER_IDS allowlist. Его автор — чат, user_id=null;
  фиктивный from Telegram не подтверждает личность владельца и его согласие.
- Поздний reply или ignore меняет failed на processed. Напоминание через 5 минут одно,
  ещё через 5 минут — failed; этот статус означает отсутствие ответа.
- Сжатие большого контекста снова работает; codex_app доступен.
- Штатный профиль :workspace сохранён, старый override never снят через
  thread/settings/update; для IPC используется обычное согласование on-request.
- Успешный pytest и тесты lifecycle-команд указаны в актуальном README.

Это не обещание, что ещё не реализованные голос, видео или отдельный MCP-инструмент
уже поддерживаются. Новую функцию сначала реализуй и проверь.

## Устройство и ошибки, которые не нужно повторять

Desktop — прямой родитель подписанного Codex CLI. Bootstrap заменяет себя CLI
через exec; дочерний Python-брокер multiplexes stdio и приватный Unix WebSocket.
Цепочка MCP Node → CLI → Desktop должна сохраняться: Python между CLI и Desktop
приводил к missing-code-signing-identity и отсутствию codex_app. Проверку подписи,
attestation и настройки организации не отключай и приложение не патчи.

Одноразовый app-server --print-startup-config, help и вспомогательные stdio-серверы
передаются штатному CLI. Изменять их stdout/exit code нельзя. Брокер готов после
успешного initialize ACK; отдельный initialized от этой версии Desktop не требуется.
Desktop отвечает на native server requests (approvals/auth/attestation), мост их
не одобряет. Все API-запросы идут в тот же живой сервер, а не во второй экземпляр.

Sandbox может блокировать connect к Unix IPC. CLI явно сообщает
ipc_permission_denied: запроси обычное согласование конкретной CLI-команды,
без sudo. delivery=not_submitted означает отказ до передачи; delivery=unknown
после send/ACK требует сохранения request_id и проверки истории. Отказ согласования
не обходи другим каналом.
