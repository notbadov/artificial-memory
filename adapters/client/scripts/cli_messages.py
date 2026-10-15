"""What the terminal client says to a person, in English and Russian.

One table with both languages, the language of the computer, and the cleaning of every value
that is shown: a name, an address or a message from the service, a file or another program
never brings an escape sequence or a direction override to a terminal.

The synchronization client has a table of its own (`sync_messages.py`): the two are delivered
apart, this one in the signed runtime and that one in a release, and decide the language the
same way.

This file is a part of `command_line.py` and lies next to it in the runtime. It imports
nothing of the client. The runtime's installer (`companion_install.py`) lies next to it as well
and says its errors from this table too.
"""

from __future__ import annotations

import locale
import os
import re

MESSAGES = {
    "product": ("Artificial Memory", "Artificial Memory"),
    "unsupported_platform": (
        "Artificial Memory in the terminal works on macOS and Windows.",
        "Artificial Memory в терминале работает на macOS и Windows."),
    "bad_origin": (
        "The Artificial Memory address must be an https:// address without parameters.",
        "Адрес Artificial Memory должен начинаться с https:// и не содержать параметров."),
    "owned_by_app": (
        "This computer is already connected by the Artificial Memory desktop app{client}.\n"
        "One computer has one connection, so nothing was changed here.\n"
        "To use the terminal instead, disconnect the computer in the app first; "
        "to keep using the app, {action} there.\n"
        "If the app was removed from this computer: `artificial-memory disconnect --without-app`.",
        "Этот компьютер уже подключён приложением Artificial Memory{client}.\n"
        "У одного компьютера одно подключение, поэтому здесь ничего не изменено.\n"
        "Чтобы работать из терминала, сначала отключите компьютер в приложении; "
        "чтобы остаться в приложении, {action} там.\n"
        "Если приложение с этого компьютера удалено: `artificial-memory disconnect --without-app`."),
    "action_sync": ("synchronize", "синхронизируйте"),
    "action_disconnect": ("disconnect", "отключайте"),
    "action_schedule": ("manage the schedule", "управляйте расписанием"),
    "action_connect": ("keep working", "продолжайте работу"),
    "action_uninstall": ("disconnect and remove it", "отключайте и удаляйте его"),
    "superseded": (
        "The desktop app has connected this computer separately ({other}). The terminal connection "
        "({client}) is still active on the server.\nRun `artificial-memory disconnect` to end the "
        "terminal connection; the app's connection is not touched.",
        "Приложение подключило этот компьютер отдельно ({other}). Подключение из терминала "
        "({client}) ещё действует на сервере.\nВыполните `artificial-memory disconnect`, чтобы "
        "завершить подключение из терминала; подключение приложения не будет затронуто."),
    "already_connected": (
        "This computer is already connected from the terminal ({client}). "
        "Run `artificial-memory status` to see it, or `artificial-memory disconnect` first.",
        "Этот компьютер уже подключён из терминала ({client}). "
        "Посмотрите состояние: `artificial-memory status`, или сначала выполните `artificial-memory disconnect`."),
    "not_connected": (
        "This computer is not connected. Run `artificial-memory connect`.",
        "Этот компьютер не подключён. Выполните `artificial-memory connect`."),
    "choose_apps": (
        "Which AI apps should Artificial Memory work with on this computer?",
        "С какими ИИ-приложениями Artificial Memory будет работать на этом компьютере?"),
    "choose_apps_hint": (
        "Enter the names separated by commas, or press Enter for [{default}]: ",
        "Введите названия через запятую или нажмите Enter, чтобы выбрать [{default}]: "),
    "apps_required": (
        "Name the AI apps to connect, for example: artificial-memory connect --app codex --app claude",
        "Укажите ИИ-приложения, например: artificial-memory connect --app codex --app claude"),
    "apps_unknown": (
        "Unknown AI app: {value}. Choose from: codex, claude, kimi, opencode, cursor.",
        "Неизвестное ИИ-приложение: {value}. Доступны: codex, claude, kimi, opencode, cursor."),
    "name_invalid": (
        "The computer name must be one line of up to 200 characters.",
        "Название компьютера — одна строка не длиннее 200 символов."),
    "service_unavailable": (
        "Artificial Memory did not answer. Check the network and try again; nothing was changed.",
        "Artificial Memory не отвечает. Проверьте сеть и повторите; ничего не изменено."),
    "authorization_unavailable": (
        "Connecting a computer through the browser is not available on this Artificial Memory server.",
        "Подключение компьютера через браузер на этом сервере Artificial Memory недоступно."),
    "authorization_without_code": (
        "Artificial Memory did not issue a confirmation code, so the connection was not started.",
        "Artificial Memory не выдал код подтверждения, поэтому подключение не начато."),
    "confirm_in_browser": (
        "Confirmation code: {code}\n\nOpen this page, sign in, and enter the code to connect «{name}»:\n  {url}\n",
        "Код подтверждения: {code}\n\nОткройте страницу, войдите и введите код, чтобы подключить «{name}»:\n  {url}\n"),
    "browser_opened": ("The page was opened in your browser. Waiting for your confirmation…",
                       "Страница открыта в браузере. Жду подтверждения…"),
    "browser_not_opened": ("Open the page yourself. Waiting for your confirmation…",
                           "Откройте страницу сами. Жду подтверждения…"),
    "authorization_denied": ("The connection was declined in the browser. Nothing was changed.",
                             "Подключение отклонено в браузере. Ничего не изменено."),
    "authorization_expired": ("The confirmation code expired. Run `artificial-memory connect` again.",
                              "Срок кода подтверждения истёк. Выполните `artificial-memory connect` ещё раз."),
    "authorization_cancelled": ("The connection was cancelled. Nothing was changed.",
                                "Подключение отменено. Ничего не изменено."),
    "authorization_lost": (
        "Artificial Memory accepted the confirmation but the connection could not be completed, and this "
        "computer holds no key for it. If a new computer appeared on the Computers page of Artificial Memory, "
        "remove it there; then run `artificial-memory connect` again. (Reason: {reason}.)",
        "Artificial Memory принял подтверждение, но подключение не завершилось, и ключа от него на этом "
        "компьютере нет. Если на странице «Компьютеры» в Artificial Memory появился новый компьютер, "
        "удалите его там; затем выполните `artificial-memory connect` ещё раз. (Причина: {reason}.)"),
    "connected_meanwhile": (
        "While the page was open this computer was connected another way (by the desktop app). "
        "The connection made here was ended; nothing of the other one was changed.",
        "Пока страница была открыта, этот компьютер подключили другим способом (приложением). "
        "Подключение, созданное здесь, завершено; другое не изменено."),
    "remove_in_console": (
        "The server still lists {client}: open Artificial Memory, the Computers page, and remove it there.",
        "На сервере по-прежнему числится {client}: откройте Artificial Memory, страницу «Компьютеры», и удалите его там."),
    "this_computer": ("this computer", "этот компьютер"),
    "app_present": (
        "The Artificial Memory desktop app is installed on this computer and holds this connection. Disconnect "
        "the computer in the app. `--without-app` is for a computer the app was removed from; nothing was changed.",
        "Приложение Artificial Memory установлено на этом компьютере и держит это подключение. Отключите "
        "компьютер в приложении. `--without-app` нужен, когда приложение уже удалено; ничего не изменено."),
    "keychain_not_allowed": (
        "The connection was saved by the desktop app in the Keychain, and macOS did not let this command read it, "
        "so access could not be revoked and nothing was changed. Run the command again and allow the access when "
        "macOS asks, or remove this computer on the Computers page of Artificial Memory.",
        "Подключение сохранено приложением в Связке ключей, и macOS не дала этой команде его прочитать, "
        "поэтому отозвать доступ не удалось и ничего не изменено. Повторите команду и разрешите доступ, когда "
        "macOS спросит, либо удалите этот компьютер на странице «Компьютеры» в Artificial Memory."),
    "revoked_by_app": (
        "The terminal connection of this computer ({client}) was ended by the Artificial Memory desktop app: the "
        "computer was connected twice, and the app keeps its own connection. Manage this computer from the app.",
        "Подключение этого компьютера из терминала ({client}) завершено приложением Artificial Memory: компьютер "
        "был подключён дважды, и приложение оставило своё подключение. Управляйте этим компьютером из приложения."),
    "superseded_unknown": ("its id is not known here yet", "его идентификатор здесь пока неизвестен"),
    "disconnected_unrevoked": (
        "Disconnected on this computer. Its access key could not be read here, so access could NOT be revoked "
        "on the server from this computer.",
        "Отключено на этом компьютере. Ключ доступа здесь прочитать не удалось, поэтому отозвать доступ "
        "на сервере с этого компьютера НЕ получилось."),
    "uninstalled_shared_kept": (
        "The terminal connection and everything of its own were removed. The runtime, the synchronization "
        "client and the release cache in {root} are kept: the Artificial Memory desktop app uses them.",
        "Подключение из терминала и всё, что относилось только к нему, удалено. Среда выполнения, программа "
        "синхронизации и кэш выпусков в {root} оставлены: ими пользуется приложение Artificial Memory."),
    "trust_unknown": (
        "Artificial Memory named a release key this client was not built with. The connection was ended "
        "and nothing was installed. Update the client: npx artificial-memory@latest connect",
        "Artificial Memory назвал ключ выпуска, которого нет в этом клиенте. Подключение завершено, "
        "ничего не установлено. Обновите клиент: npx artificial-memory@latest connect"),
    "trust_changed": (
        "The release key stored on this computer differs from the one Artificial Memory uses now. "
        "It is never replaced automatically; the connection was ended and nothing was installed.",
        "Ключ выпуска, сохранённый на этом компьютере, отличается от ключа Artificial Memory. "
        "Он не заменяется автоматически; подключение завершено, ничего не установлено."),
    "credential_not_saved": (
        "The access key of this computer could not be saved, so the connection was ended. Nothing was installed.",
        "Не удалось сохранить ключ доступа этого компьютера, поэтому подключение завершено. Ничего не установлено."),
    "credential_unreadable": (
        "The access key of this computer ({client}) could not be read. Run `artificial-memory disconnect`: it "
        "removes the connection from this computer and says how to remove it on the server; then connect again.",
        "Не удалось прочитать ключ доступа этого компьютера ({client}). Выполните `artificial-memory disconnect`: "
        "команда уберёт подключение с этого компьютера и скажет, как убрать его на сервере; затем подключитесь заново."),
    "connected": ("Connected: «{name}» ({client}).", "Подключено: «{name}» ({client})."),
    "installing": (
        "Checking the signature of the approved release and installing skills, rules and tools…",
        "Проверяю подпись одобренного выпуска и устанавливаю навыки, правила и инструменты…"),
    "install_failed": (
        "The computer is connected, but the installation did not finish{detail}.\n"
        "Run `artificial-memory sync` to finish it; no second connection will be created.",
        "Компьютер подключён, но установка не завершилась{detail}.\n"
        "Выполните `artificial-memory sync`, чтобы завершить её; второе подключение не появится."),
    "release_too_old": (
        "The release approved for your space does not support the terminal client yet, so nothing can be "
        "installed. The connection made here was ended; connect again after the next release.",
        "Выпуск, одобренный для вашего пространства, ещё не поддерживает работу из терминала, поэтому "
        "установить ничего нельзя. Подключение, созданное здесь, завершено; подключитесь после следующего выпуска."),
    "release_too_old_later": (
        "The release approved for your space does not support the terminal client. Nothing was changed.",
        "Выпуск, одобренный для вашего пространства, не поддерживает работу из терминала. Ничего не изменено."),
    "release_without_cursor_later": (
        "The release approved for your space does not support Cursor yet. Nothing was changed.",
        "Выпуск, одобренный для вашего пространства, пока не поддерживает Cursor. Ничего не изменено."),
    "syncing": ("Synchronizing…", "Синхронизирую…"),
    "sync_done": ("Synchronized. Restart the AI apps that were open so they pick up the changes.",
                  "Синхронизировано. Перезапустите открытые ИИ-приложения, чтобы они увидели изменения."),
    "sync_busy": ("A synchronization is already running on this computer; this one was not started.",
                  "Синхронизация на этом компьютере уже идёт; новая не запущена."),
    "sync_failed": ("Synchronization did not finish (stage: {stage}). {message}",
                    "Синхронизация не завершилась (этап: {stage}). {message}"),
    "sync_repair_hint": (
        "To try this release once more on this computer, run `artificial-memory sync --repair`.",
        "Чтобы ещё раз попробовать установить этот выпуск на этом компьютере, выполните "
        "`artificial-memory sync --repair`."),
    "sync_failed_hint": ("Nothing else was changed. Run `artificial-memory status` for details.",
                         "Остальное не менялось. Подробности: `artificial-memory status`."),
    "client_missing": (
        "The synchronization client is not installed yet. Run `artificial-memory sync` to install it.",
        "Клиент синхронизации ещё не установлен. Выполните `artificial-memory sync`, чтобы установить его."),
    "consent_waiting": ("Waiting for your approval ({count}):", "Ждут вашего согласия ({count}):"),
    "consent_skill": ("  skill «{title}» can run commands or programs on this computer",
                      "  навык «{title}» может запускать команды или программы на этом компьютере"),
    "consent_mcp": ("  server «{title}» ({detail}) asks to run its tools without asking each time",
                    "  сервер «{title}» ({detail}) просит запускать инструменты без подтверждения каждый раз"),
    "consent_ask": ("  Allow? [y/N] ", "  Разрешить? [y/N] "),
    "consent_hint": (
        "Nothing is approved without you. Run `artificial-memory consents` in a terminal to decide.",
        "Без вас ничего не одобряется. Выполните `artificial-memory consents` в терминале, чтобы решить."),
    "consent_none": ("Nothing waits for your approval.", "Ничего не ждёт вашего согласия."),
    "consent_recorded": ("  Approved. It is installed by the next synchronization.",
                         "  Разрешено. Установится при следующей синхронизации."),
    "consent_declined": ("  Not approved. It stays uninstalled.", "  Не разрешено. Остаётся неустановленным."),
    "consent_failed": ("  The approval could not be recorded.", "  Не удалось записать согласие."),
    "schedule_on": ("Background synchronization is on: once an hour.",
                    "Фоновая синхронизация включена: раз в час."),
    "schedule_off": ("Background synchronization is off. Synchronize with `artificial-memory sync`.",
                     "Фоновая синхронизация выключена. Синхронизируйте командой `artificial-memory sync`."),
    "schedule_needs_update": (
        "The installed synchronization client cannot run without the background schedule yet. "
        "Run `artificial-memory sync` to update it and repeat.",
        "Установленный клиент синхронизации пока не умеет работать без фонового расписания. "
        "Выполните `artificial-memory sync`, чтобы обновить его, и повторите."),
    "schedule_failed": ("The background schedule could not be changed: {detail}",
                        "Не удалось изменить фоновое расписание: {detail}"),
    "schedule_stale": (
        "Background synchronization: the job still starts the previous runtime. "
        "Run `artificial-memory sync` to move it to the current one.",
        "Фоновая синхронизация: задача ещё запускает прежнюю среду выполнения. "
        "Выполните `artificial-memory sync`, чтобы перевести её на текущую."),
    "disconnect_busy": ("A synchronization is running. Wait for it to finish and repeat.",
                        "Идёт синхронизация. Дождитесь её окончания и повторите."),
    "disconnect_not_recorded": (
        "Artificial Memory did not answer and the disconnection could not be kept for later. "
        "The computer stays connected; try again.",
        "Artificial Memory не ответил, а сохранить отключение до его ответа не удалось. "
        "Компьютер остаётся подключённым; повторите попытку."),
    "disconnected": ("Disconnected. Access is revoked on the server and removed from the AI apps on this computer.",
                     "Отключено. Доступ отозван на сервере и убран из ИИ-приложений на этом компьютере."),
    "disconnected_pending": (
        "Disconnected on this computer. Artificial Memory did not answer, so access is not revoked on the "
        "server yet: it is revoked by the next `artificial-memory` command that reaches the server. "
        "Run `artificial-memory status` when you are online.",
        "Отключено на этом компьютере. Artificial Memory не ответил, поэтому доступ на сервере ещё не отозван: "
        "его отзовёт следующая команда `artificial-memory`, которая достучится до сервера. "
        "Выполните `artificial-memory status`, когда появится сеть."),
    "local_access_left": (
        "Some settings of AI apps could not be cleaned automatically: {apps}.",
        "Настройки некоторых ИИ-приложений не удалось очистить автоматически: {apps}."),
    "revocation_delivered": ("Access of an earlier connection ({client}) is now revoked on the server.",
                             "Доступ прежнего подключения ({client}) отозван на сервере."),
    "revocation_waiting": (
        "Access of an earlier connection ({client}) is not revoked on the server yet; it is retried by every command.",
        "Доступ прежнего подключения ({client}) на сервере ещё не отозван; каждая команда повторяет попытку."),
    "uninstall_pending": (
        "Not removed yet: access of {client} is not revoked on the server, and its key is kept only for that. "
        "Repeat `artificial-memory uninstall` when you are online. To delete the key anyway, run "
        "`artificial-memory uninstall --forget-pending` and remove the computer in Artificial Memory yourself.",
        "Пока не удалено: доступ {client} на сервере не отозван, и ключ хранится только ради этого. "
        "Повторите `artificial-memory uninstall`, когда появится сеть. Чтобы удалить ключ всё равно, выполните "
        "`artificial-memory uninstall --forget-pending` и удалите компьютер в Artificial Memory сами."),
    "uninstalled": (
        "Removed from this computer: the background job, the access key, the runtime, the synchronization client "
        "and its caches ({root}).\nKept: your decisions about skills with programs and the list of withdrawn "
        "releases (two small files in {root}/client), and the skills of the platform already copied into the AI apps.",
        "Удалено с этого компьютера: фоновая задача, ключ доступа, среда выполнения, программа синхронизации "
        "и её кэш ({root}).\nОставлено: ваши решения о навыках с программами и список отозванных выпусков "
        "(два небольших файла в {root}/client), а также навыки платформы, уже скопированные в ИИ-приложения."),
    "status_none": ("Not connected.", "Не подключён."),
    "status_terminal": ("Connected from the terminal: «{name}» ({client}).",
                        "Подключён из терминала: «{name}» ({client})."),
    "status_app": ("Connected by the desktop app{client}. The app synchronizes this computer.",
                   "Подключён приложением{client}. Этот компьютер синхронизирует приложение."),
    "status_apps": ("AI apps: {apps}", "ИИ-приложения: {apps}"),
    "status_installing": ("Installation is not finished. Run `artificial-memory sync`.",
                          "Установка не завершена. Выполните `artificial-memory sync`."),
    "status_running": ("A synchronization is running now.", "Сейчас идёт синхронизация."),
    "status_last_ok": ("Last synchronization: {at}, succeeded.", "Последняя синхронизация: {at}, успешно."),
    "status_last_failed": ("Last synchronization: {at}, failed{stage}.",
                           "Последняя синхронизация: {at}, с ошибкой{stage}."),
    "status_never": ("No synchronization has finished yet.", "Ни одна синхронизация ещё не завершилась."),
    "status_schedule_on": ("Background synchronization: on, once an hour ({job}).",
                           "Фоновая синхронизация: включена, раз в час ({job})."),
    "status_schedule_off": ("Background synchronization: off.", "Фоновая синхронизация: выключена."),
    "status_schedule_broken": (
        "Background synchronization: turned on, but the job is not registered. Run `artificial-memory sync`.",
        "Фоновая синхронизация: включена, но задача не зарегистрирована. Выполните `artificial-memory sync`."),
    "status_consents": ("Waiting for your approval: {count}. Run `artificial-memory consents`.",
                        "Ждут вашего согласия: {count}. Выполните `artificial-memory consents`."),
    # Written by the synchronization when a Codex here is too old for "writes" (sync_codex_versions.py).
    "codex_update_old": (
        "Codex {version} on this computer is older than {since}, so the tools of {servers} ask before "
        "every call, reads included. Update Codex to {since} or later, then run `artificial-memory sync`.",
        "Codex {version} на этом компьютере старше {since}, поэтому инструменты {servers} спрашивают "
        "разрешение перед каждым вызовом, даже при чтении. Обновите Codex до {since} или новее, затем "
        "выполните `artificial-memory sync`."),
    "codex_update_unknown": (
        "A Codex on this computer did not report its version, so the tools of {servers} ask before every "
        "call, reads included. Update Codex to {since} or later, then run `artificial-memory sync`.",
        "Один из Codex на этом компьютере не сообщил свою версию, поэтому инструменты {servers} спрашивают "
        "разрешение перед каждым вызовом, даже при чтении. Обновите Codex до {since} или новее, затем "
        "выполните `artificial-memory sync`."),
    "usage_tty": ("This command asks a question and needs a terminal.",
                  "Эта команда задаёт вопрос, и ей нужен терминал."),
    # The runtime's installer (companion_install.py), started by the desktop application and by
    # this client.
    "install_platform_unsupported": (
        "The synchronization client is installed on macOS and Windows only.",
        "Клиент синхронизации устанавливается только на macOS и Windows."),
    "install_bundle_changed": (
        "The verified bundle changed after it was downloaded; installation stopped",
        "Проверенная сборка изменилась после загрузки; установка остановлена"),
    "install_bundle_without_client": (
        "The verified bundle has no synchronization client", "В проверенной сборке нет клиента синхронизации"),
}


def language() -> str:
    for name in ("ARTIFICIAL_MEMORY_LANG", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(name)
        if value:
            return "ru" if value.lower().startswith("ru") else "en"
    try:
        value = locale.getlocale()[0] or ""
    except ValueError:
        value = ""
    return "ru" if value.lower().startswith(("ru", "russian")) else "en"


# Controls (C0, DEL, C1), direction marks and overrides, line and paragraph separators, and other
# invisible characters (soft hyphen, combining grapheme joiner, Hangul fillers, variation
# selectors, the tag block): everything that can move the cursor, recolour, reorder or hide
# part of what a terminal shows.
UNPRINTABLE = re.compile(
    "[\x00-\x1f\x7f-\x9f\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f\u2028-\u202f"
    "\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff9-\ufffb\U000e0000-\U000e0fff]")


def clean(value: object) -> str:
    """A value as it may be shown in a terminal. Names, addresses and messages come from the
    service, from files and from other programs; none of them may bring an escape sequence or a
    direction override to the screen. Each such character is shown as a visible mark."""
    return UNPRINTABLE.sub("\N{REPLACEMENT CHARACTER}", str(value))


def text(key: str, **values: object) -> str:
    """A message of this client with its values filled in. Every value is cleaned here, so no
    caller can forget to."""
    english, russian = MESSAGES[key]
    return (russian if language() == "ru" else english).format(**{name: clean(value) for name, value in values.items()})
