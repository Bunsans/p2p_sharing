# Платёжная часть P2P-аренды

Подключаемое Django-приложение для основного backend. Пользователь подтвердил
Django, PostgreSQL и округление HALF_UP; приложение разрабатывается независимо,
пока основной backend ещё отсутствует. `testproject` — локальный проект для
разработки и проверок. Он не предназначен для развёртывания в production.

Спецификация: [issue #1](https://github.com/Bunsans/p2p_sharing/issues/1).
Термины: [GLOSSARY.md](GLOSSARY.md). Решения: [docs/adr](docs/adr/).

## Что реализовано

- Снимок условий аренды, отдельные начисления аренды и залога, попытки оплаты
  и сохраняемые команды исполнения. Изменённые условия той же брони отклоняются.
- Повтор команды возвращает прежнюю попытку; изменённые параметры с прежним
  ключом вызывают `CommandConflict`. Неизвестный исход не разрешает новое списание.
- Наценка 20% и накопительные частичные возвраты в целых копейках с HALF_UP;
  распределение залога и подсчёт суммарных дополнительных получасов.
- Исполнение команд через явный имитатор провайдера, без сетевых запросов.
  Django Admin позволяет только просматривать финансовую историю.

Это часть [задачи #5](https://github.com/Bunsans/p2p_sharing/issues/5), а не её
полное завершение. Выплаты, исполнение возвратов, webhook, чеки, интеграция
выдачи/отмены с основным backend и настоящая песочница ещё не реализованы.
Возвраты пока представлены только расчётом сумм. Денежная ставка дополнительного
времени и вычет оплаченного покрытия также остаются следующими шагами.

## Запуск через Docker

Нужны Docker с Compose и запущенный Docker Engine — например, Docker Desktop
или Colima (`colima start`). Python, uv и PostgreSQL на хосте не требуются.

```sh
docker compose up --build --wait
```

Compose поднимает PostgreSQL 17, ждёт его готовности, применяет миграции и
запускает приложение. Команда завершается после проверки готовности Admin;
он доступен на <http://localhost:8000/admin/>.
Создание пользователя и просмотр журнала:

```sh
docker compose exec app python manage.py createsuperuser
docker compose logs -f app
```

Образ использует Python 3.12 и зависимости из `uv.lock`; приложение запускается
не от root. Это окружение разработки: `runserver --insecure` обслуживает статику
Admin при `DEBUG=False`, используются локальные тестовые настройки и пароль БД.
Приложение доступно только на loopback-интерфейсе хоста; порт PostgreSQL наружу
не публикуется. В образ попадают только исходники, тесты и файлы зависимостей.

Проверки запускаются в том же образе и используют отдельную тестовую БД:

```sh
docker compose run --rm app python manage.py check
docker compose run --rm app python manage.py test --noinput
docker compose run --rm app python manage.py makemigrations --check --dry-run
```

Имитатор оплаты запускается явно, после создания команд:

```sh
docker compose run --rm app python manage.py run_payment_worker --simulate --limit 100
```

После изменения кода пересоберите приложение; файлы хоста не монтируются внутрь
контейнера:

```sh
docker compose up --build --wait app
```

Остановка сохраняет данные в именованном Docker volume `postgres_data`:

```sh
docker compose down
```

`docker compose down -v` удаляет этот volume вместе с данными. Прежняя локальная
БД из `.postgres/` не используется и автоматически не переносится в контейнер.

Чистые расчёты при наличии Python можно проверить без Docker и БД:

```sh
python3 -m unittest tests.test_calculations -v
```

## Рабочий контракт подключения

В основной проект добавляются `payments` в `INSTALLED_APPS` и миграции приложения.
Приведённый контракт реализован локально; согласование всех деталей с основной
командой в [#3](https://github.com/Bunsans/p2p_sharing/issues/3) ещё не завершено.

`prepare_settlement(*, booking_ref, renter_ref, owner_ref, terms_version,
owner_kopecks, deposit_kopecks, starts_at, ends_at, currency="RUB")` возвращает
`Settlement`. Ссылки на бронь и участников — непрозрачные непустые строки до
128 символов, без внешних ключей на отсутствующие модели основного проекта.
Версия — положительное целое, суммы — целые копейки, валюта — RUB, даты —
с часовым поясом. Наценка вычисляется приложением. Повтор одинаковых условий
возвращает прежний снимок; изменение снимка отклоняется.

`start_payment(*, command_id, charge_id, actor_ref, expected_terms_version,
handover_allowed)` возвращает `PaymentAttempt`. Первые два аргумента — объекты
`UUID`, `actor_ref` — доверенная ссылка на арендатора, версия — положительное
целое, `handover_allowed` — именно `bool` от backend. Нулевое начисление
не создаёт банковскую операцию. Возвращённая попытка ещё не означает оплату:
успех подтверждается только её статусом `succeeded`.

Вызывающий backend аутентифицирует участника, проверяет его права и разрешение
передачи под блокировкой брони. Он держит внешнюю транзакцию открытой до окончания
`start_payment`; порядок блокировок — бронь → расчёт → начисление. Платёжный
модуль дополнительно проверяет арендатора и версию. Пользовательский HTTP-запрос
не должен напрямую задавать `actor_ref`, сумму или `handover_allowed`.

Ошибочные типы и значения вызывают `TypeError`/`ValueError`, конфликт ключа или
условий — `CommandConflict`, отсутствие разрешения — `PermissionDenied`.
Основной проект определяет их отображение в своём API.

Пример для изолированного локального проекта после миграций: создаём аренду
800 + 160 = 960 ₽ с отдельным залогом 3 000 ₽ и ставим оплату аренды в очередь.
Здесь разрешение передачи имитируется доверенным кодом примера.

```sh
docker compose run --rm -T app python manage.py shell <<'PY'
from datetime import datetime, timedelta, timezone
from uuid import UUID
from payments.models import Charge
from payments.services import prepare_settlement, start_payment

starts_at = datetime(2026, 11, 10, 10, tzinfo=timezone.utc)
settlement = prepare_settlement(
    booking_ref="demo-booking", renter_ref="demo-renter", owner_ref="demo-owner",
    terms_version=1, owner_kopecks=80_000, deposit_kopecks=300_000,
    starts_at=starts_at, ends_at=starts_at + timedelta(days=1), currency="RUB",
)
charge = settlement.charges.get(purpose=Charge.Purpose.RENT)
attempt = start_payment(
    command_id=UUID("3d5ee712-7bab-4eb7-8ae3-81b9344fcd99"),
    charge_id=charge.id, actor_ref="demo-renter", expected_terms_version=1,
    handover_allowed=True,
)
print(attempt.id, attempt.status)
PY

docker compose run --rm app python manage.py run_payment_worker --simulate --limit 100
```

Исполнитель обрабатывает один ограниченный пакет и завершается; это не daemon.
Имитатор даёт воспроизводимый результат для сохранённого ключа. Он не использует
реквизиты и не подтверждает работоспособность ЮKassa или другого банка.

Команда хранит UUID ключа повтора, исходное тело и время первой потенциальной
отправки. Захват с ограниченным сроком (`lease`) выполняется в короткой
транзакции; вызов провайдера происходит после её окончания. При повторном
захвате сохраняются прежние ключ и тело. Без известного внешнего ID
автоматическое создание останавливается через 23 часа 59 минут после первой
потенциальной отправки — с минутным запасом до окна 24 часа. При известном ID
сверка чтением состояния продолжается и после этого срока. Запоздалый ответ
сохраняется отдельным наблюдением и не откатывает состояние нового исполнителя.

Расчёты и примеры округления описаны в [ADR-0005](docs/adr/0005-money-rounding.md).
Суммы меньше рубля сохраняются точно; допустимый способ их банковского исполнения
предстоит подтвердить в [#2](https://github.com/Bunsans/p2p_sharing/issues/2).
