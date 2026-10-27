# GoIT CS — домашнє завдання 5

Два скрипти до модуля «Асинхронна обробка»:

- `async_sorter.py` — рекурсивно копіює файли в папки за розширенням;
- `mapreduce_words.py` — завантажує текст за URL, рахує слова через MapReduce
  і будує графік найчастіших слів.

## Запуск

Потрібні [uv](https://docs.astral.sh/uv/getting-started/installation/) і Make.
Проєкт використовує Python 3.14. Команди виконуйте з кореня проєкту в Bash
на macOS або Linux:

```bash
make init
make demo
```

Перша команда встановлює залежності з `uv.lock`. Друга запускає обидва завдання
на локальних прикладах і зберігає графік та JSON-звіти в `artifacts/demo/`.
Для встановлення пакетів потрібна мережа; саме demo працює без зовнішніх сайтів.

Без Make: `uv sync --locked`, потім `uv run --locked python -m scripts.demo`.

## Завдання 1. Сортування файлів

```bash
uv run --locked python async_sorter.py examples/source artifacts/sorted
```

Результат для порожньої цільової папки:

```text
artifacts/sorted/
├── csv/table.csv
├── json/data.json
├── no_extension/README
└── txt/
    ├── notes.TXT
    ├── report.txt
    └── report_1.txt
```

Оригінали залишаються на місці. Регістр розширення не має значення, а однакові
імена отримують суфікси `_1`, `_2` тощо. Повторний запуск створює нові копії.
Символічні посилання пропускаються. Вхідна й вихідна папки не можуть містити одна одну.

Додаткові параметри: `--workers 4`, `--log-file artifacts/sorter.log`,
`--report artifacts/sorter.json`. Помилки потрапляють у журнал; доступні файли
продовжують копіюватися. [Як працює сортування](docs/async-sorter.md).

## Завдання 2. Частота слів

```bash
uv run --locked python mapreduce_words.py \
  https://www.gutenberg.org/files/1342/1342-0.txt \
  --top 10 --workers 4 --output artifacts/top_words.png
```

Без URL скрипт бере той самий текст *Pride and Prejudice*. Можна передати іншу
HTTP(S) адресу звичайного тексту. Найчастіші слова та їхні частоти виводяться
в термінал, графік зберігається у PNG. Найчастіше слово розташоване вгорі.

Підрахунок не враховує регістр, числа й пунктуацію. Українські літери та апострофи
підтримуються; слова на кшталт `the` та `and` залишаються. `--show` відкриває вікно
графіка, якщо доступне GUI. `--report artifacts/words.json` зберігає частоти
й дані про завантажений текст. [Алгоритм і параметри](docs/mapreduce.md).

## Перевірки

```bash
make check            # Ruff, Markdown, mypy, тести й coverage
make check-rocky      # перевірки та demo в Rocky Linux через Docker
make validate-reports # перевірка JSON після make demo
make benchmark        # порівняння з послідовними алгоритмами
make audit            # перевірка залежностей; потрібна мережа
```

Останній повний прогін 27.10.2026: 187 тестів пройшли на macOS і Rocky Linux,
покриття з урахуванням гілок — 98,86%. [Результати перевірок](docs/verification.md).

## Документація

- [Розробка й тести](docs/development.md)
- [Запуск у Rocky Linux](docs/rocky-linux.md)
- [JSON-звіти та їх перевірка](docs/reports.md)
- [Вимірювання швидкодії](docs/benchmark.md)
- [Виправлення та відповідність завданню](docs/review.md)
