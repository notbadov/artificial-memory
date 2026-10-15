"""What the synchronization client says to a person, in English and in Russian.

One table for the whole client, so that its code holds no sentence of its own and a person
reads one language. The table holds texts meant for a person: an error named in a report or
raised to whoever started the run. Whatever a program reads is not here and never changes
with the language: stage and reason codes, the keys of an answer, the values kept in files.

This file is a part of `sync_client.py`, the entry point, and the first one: it imports nothing
of the client, and every other part may import it.
"""

from __future__ import annotations

import locale
import os

# key -> (English, Russian). A text takes its values by name, the same names in both languages.
MESSAGES: dict[str, tuple[str, str]] = {
    "rules_malformed": ("instruction rules from the service are malformed",
                        "правила для инструкций, полученные от сервиса, составлены неверно"),
    "rules_not_placed": ("instruction rules: {error}", "правила для инструкций: {error}"),
    "withdrawn_list_malformed": ("the release's list of withdrawn releases is malformed",
                                 "список отозванных выпусков в этом выпуске составлен неверно"),
    "release_withdrawn": ("release {release_id} was withdrawn by the platform and is not installed",
                          "выпуск {release_id} отозван платформой и не устанавливается"),
    "client_release_refused": (
        "The synchronization client of release {release_id} did not start on this computer twice. It is not "
        "installed a third time: the client that was here before stays, and the next approved release is "
        "installed as usual.",
        "Клиент синхронизации выпуска {release_id} дважды не запустился на этом компьютере. В третий раз он "
        "не устанавливается: остаётся прежний клиент, а следующий одобренный выпуск установится как обычно."),
    "client_release_refused_after_request": (
        "The synchronization client of release {release_id} does not start on this computer, also after the "
        "repair you asked for. It is not installed again by itself: the client that was here before stays and "
        "keeps working, and the next approved release is installed as usual. If you need this release sooner, "
        "connect this computer again or write to support.",
        "Клиент синхронизации выпуска {release_id} не запускается на этом компьютере — и после восстановления, "
        "которое вы запросили. Сам он больше не устанавливается: остаётся и работает прежний клиент, а следующий "
        "одобренный выпуск установится как обычно. Если этот выпуск нужен раньше, переподключите компьютер или "
        "напишите в поддержку."),
    "client_repair_on_request_failed": (
        "The repair you asked for was tried and did not work: the synchronization client of release {release_id} "
        "still does not start on this computer. The client that was here before stays and keeps working. This "
        "release is not tried again by itself; the next approved release is installed as usual. If you need this "
        "release sooner, connect this computer again or write to support.",
        "Восстановление, которое вы запросили, выполнено, но не помогло: клиент синхронизации выпуска {release_id} "
        "по-прежнему не запускается на этом компьютере. Остаётся и работает прежний клиент. Сам этот выпуск больше "
        "устанавливаться не будет; следующий одобренный выпуск установится как обычно. Если этот выпуск нужен "
        "раньше, переподключите компьютер или напишите в поддержку."),
    "client_files_busy": (
        "Another program was holding a file of the synchronization client, most often an antivirus that was "
        "checking it, so the client could not be updated this time. Nothing is wrong with the release and nothing "
        "is held against it: the next synchronization tries again. If this keeps happening, add the folder "
        ".agent-platform in your home folder to the exclusions of your antivirus.",
        "Файл клиента синхронизации был занят другой программой — чаще всего антивирусом, который его проверял, — "
        "поэтому в этот раз обновить клиент не удалось. С выпуском всё в порядке, и неудачей это не считается: "
        "следующая синхронизация повторит попытку. Если так происходит постоянно, добавьте папку .agent-platform "
        "в вашей домашней папке в исключения антивируса."),
    "client_repair_failed": (
        "The synchronization client could not be installed again from the approved release. The client that "
        "was here before stays.",
        "Не удалось заново установить клиент синхронизации из одобренного выпуска. Остаётся прежний клиент."),
    # What a run says when it stops, one sentence per stage (control_plane_sync.py).
    "stage_mcp_preflight": (
        "Could not get or check the memory and context connections. The agents' settings were not changed.",
        "Не удалось получить или проверить подключения памяти и контекста. Настройки агентов не менялись."),
    "stage_signed_release": (
        "Could not check the signed bundle. Installation was not started.",
        "Не удалось проверить подписанную сборку. Установка не начата."),
    "stage_command_claim": (
        "Could not get the command: another run may already be carrying it out.",
        "Не удалось получить команду: возможно, её уже выполняет другой запуск."),
    "stage_provider_installation": (
        "Installing the agents did not finish completely. Check the individual results.",
        "Установка агентов завершилась не полностью. Проверьте отдельные результаты."),
    "stage_mcp_configuration": (
        "Could not set up memory or context for the agents.",
        "Не удалось настроить память или контекст для агентов."),
    "stage_managed_mcp_configuration": (
        "Could not synchronize the agents' external tools.",
        "Не удалось синхронизировать внешние инструменты агентов."),
    "stage_app_agent_channels": (
        "Could not connect the AI apps to tasks, memory and context as agents.",
        "Не удалось подключить ИИ-приложения к задачам, памяти и контексту как агентов."),
    "stage_scheduled_task": (
        "Could not set up the background check for updates.",
        "Не удалось настроить фоновую проверку обновлений."),
    "stage_report": (
        "The changes were made, but Control Plane did not confirm that it received the result.",
        "Изменения выполнены, но Control Plane не подтвердил получение результата."),
    # Why a stage stopped (control_plane_sync.py); the stage sentence above is what a run answers.
    "mcp_connections_missing": (
        "The MCP connections Control Plane needs are not set up; installation was not started",
        "В Control Plane не настроены необходимые MCP-подключения; установка не начата"),
    "mcp_address_invalid": (
        "An MCP address must use HTTPS without embedded credentials",
        "Адрес MCP должен использовать HTTPS без встроенных учётных данных"),
    "agent_memory_organization_missing": (
        "Control Plane did not return the Agent Memory organization",
        "Control Plane не вернул организацию Agent Memory"),
    "bundle_changed_after_download": (
        "The bundle changed after it was downloaded; installation cancelled",
        "Сборка изменилась после загрузки; установка отменена"),
    "release_without_full_installation": (
        "The release does not support the full Control Plane installation; the configuration was not changed",
        "Релиз не поддерживает полную установку Control Plane; конфигурация не изменена"),
    "protocol_unsupported": (
        "Control Plane does not support the full installation protocol yet",
        "Control Plane пока не поддерживает полный протокол установки"),
    "scheduled_task_unconfirmed": (
        "The background check of the client is not confirmed",
        "Фоновая проверка клиента не подтверждена"),
    "providers_not_installed": (
        "Not every provider was installed; the command is not finished",
        "Не все провайдеры установлены; команда не завершена"),
    # The entry point (sync_client.py).
    "release_without_sync_client": (
        "The bundle has no synchronization client", "В сборке отсутствует клиент синхронизации"),
    "release_without_client_modules": (
        "The bundle lacks required modules of the client: {modules}",
        "В сборке отсутствуют обязательные модули клиента: {modules}"),
    "verified_bundle_changed": (
        "The verified bundle changed; installation cancelled", "Проверенная сборка изменилась; установка отменена"),
    "control_plane_arguments_required": (
        "Control Plane needs a client-id, an HTTPS URL and a trusted release-trust-file; replacing the bundle "
        "locally is not allowed.",
        "Control Plane требует client-id, HTTPS URL и доверенный release-trust-file; локальная подмена сборки "
        "запрещена."),
    "companion_owns_schedule": (
        "With --scheduler companion the schedule belongs to Desktop Companion; no background task of the "
        "system is set up.",
        "При --scheduler companion расписанием владеет Desktop Companion; фоновую задачу ОС не ставим."),
    "manual_schedule_off": (
        "With --scheduler manual the background schedule is off; no background task of the system is set up.",
        "При --scheduler manual фоновое расписание выключено; фоновую задачу ОС не ставим."),
    "desired_state_failed": (
        "Could not get the approved version. Check the connection and access to Control Plane; the installation "
        "was not changed.",
        "Не удалось получить одобренную версию. Проверьте связь и доступ к Control Plane; установка не менялась."),
    "run_failed_action": (
        "Open Artificial Memory and check the client's last error.",
        "Откройте Artificial Memory и проверьте последнюю ошибку клиента."),
    "sync_already_running": (
        "Synchronization is already running for this user", "Синхронизация уже выполняется в этом профиле пользователя"),
    # The error of an app or of the background check in a report; the console shows it as it is.
    "app_install_unconfirmed": ("The installation is not confirmed", "Установка не подтверждена"),
    "app_sync_unfinished": (
        "Synchronization did not finish; try again after checking the settings",
        "Синхронизация не завершена; повторите после проверки настроек"),
    "app_not_on_computer": ("The app is not found on this computer", "Приложение не найдено на этом компьютере"),
    "app_config_unreadable": (
        "The app's MCP config (~/.cursor/mcp.json for Cursor) could not be read and was left as it is ({reason})",
        "Настройки MCP приложения (~/.cursor/mcp.json у Cursor) не прочитаны и оставлены как есть ({reason})"),
    "app_without_installer": (
        "This version has no installer for the app", "В этой версии нет установщика для приложения"),
    "background_check_unconfirmed": ("The background check is not confirmed", "Фоновая проверка не подтверждена"),
    # The hourly job and ending a connection (sync_launchers.py, sync_schedule.py, sync_disconnect.py).
    "keychain_service_invalid": ("The keychain service name is not valid", "Некорректное имя службы связки ключей"),
    "windows_protection_failed": (
        "Could not protect the client's access with Windows DPAPI",
        "Не удалось защитить доступ клиента через Windows DPAPI"),
    "macos_schedule_needs_service": (
        "The background check on macOS needs Control Plane", "Фоновая проверка macOS требует Control Plane"),
    "disconnect_arguments_required": (
        "Disconnecting needs the client-id and the address of Control Plane",
        "Для отключения нужны client-id и адрес Control Plane"),
    # What an AI app shows when the local bridge cannot reach a service for it (mcp_bridge.py).
    # The desktop application names its button in Russian only, so the English sentences say
    # what to do without naming the button.
    "bridge_service_tasks": ("Artificial Memory tasks", "Задачи Artificial Memory"),
    "bridge_service_memory": ("Artificial Memory project memory", "Память проекта Artificial Memory"),
    "bridge_service_context": ("Artificial Memory project context", "Контекст проекта Artificial Memory"),
    "bridge_agent_paused": (
        "{service} is paused for this app by the space owner. To resume it, open Artificial Memory on the web, "
        "go to Computers and open this computer's card.",
        "{service}: владелец пространства приостановил доступ для этого приложения. Чтобы возобновить его, "
        "откройте Artificial Memory в браузере, перейдите в раздел «Компьютеры» и откройте карточку этого "
        "компьютера."),
    "bridge_agent_revoked": (
        "{service}: access for this app was revoked by the space owner. Ask the owner, or connect this app "
        "again in the Artificial Memory app on this computer.",
        "{service}: владелец пространства отозвал доступ для этого приложения. Обратитесь к владельцу или "
        "снова подключите это приложение в приложении Artificial Memory на этом компьютере."),
    "bridge_agent_other_connection": (
        "{service}: this app was connected under a different account on this computer. Open the Artificial "
        "Memory app here and synchronize this computer. If this app should have access, select it on the "
        "computer's card on the web.",
        "{service}: это приложение подключено на этом компьютере под другой учётной записью. Откройте здесь "
        "приложение Artificial Memory и нажмите «Синхронизировать сейчас». Если у этого приложения должен "
        "быть доступ, выберите его в карточке компьютера в браузере."),
    "bridge_not_connected": (
        "{service} is not connected for this app. Open the Artificial Memory app on this computer and "
        "synchronize it. Code: {reason}.",
        "{service}: не подключено для этого приложения. Откройте приложение Artificial Memory на этом "
        "компьютере и нажмите «Синхронизировать сейчас». Код: {reason}."),
}


def language() -> str:
    """"ru" on a computer whose language is Russian, "en" on any other.

    The terminal client decides the same way (`language` in cli_messages.py) and the two are
    kept identical, so everything a person reads on one computer is in one language."""
    for name in ("ARTIFICIAL_MEMORY_LANG", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(name)
        if value:
            return "ru" if value.lower().startswith("ru") else "en"
    try:
        value = locale.getlocale()[0] or ""
    except ValueError:
        value = ""
    return "ru" if value.lower().startswith(("ru", "russian")) else "en"


def text(key: str, **values: object) -> str:
    """A message of this client with its values filled in.

    Values go in as they are. What this client says travels inside a JSON answer or an
    exception; whoever puts it on a screen cleans it for that screen, as the terminal client
    does."""
    english, russian = MESSAGES[key]
    return (russian if language() == "ru" else english).format(**values)
