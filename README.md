# QuickShot

Приложение для Windows: скриншоты по горячей клавише и редактор с рисованием, стрелками, текстом и размытием.

## Запуск из исходников

Нужны Windows, Python 3.12 и Microsoft Edge WebView2 Runtime.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python quickshot.py
```

По умолчанию снимок вызывается клавишей **Print Screen**. Приложение работает в системном трее. Снимки сохраняются в `%USERPROFILE%\Pictures\Screenshots`, настройки — в `%APPDATA%\QuickShot`.

## Сборка

В активированном виртуальном окружении:

```powershell
python -m pip install pyinstaller
.\build.bat
```

Результат: `dist\QuickShot\QuickShot.exe`. Для переноса приложения нужна вся папка `dist\QuickShot`, включая `_internal`.

Готовые сборки, временные снимки и локальные настройки не входят в исходники репозитория.
