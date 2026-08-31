# معماری NEXUS Platform — فاز ۱

## هدف فاز

در فاز اول، NEXUS از یک ربات Telegram با منطق پراکنده به یک هسته مستقل و قابل کنترل تبدیل می‌شود. Telegram فقط یک connector است و نباید مدل‌های دامنه، صف کار، حافظه یا سیاست‌های self-healing را مالک شود. این مرزبندی امکان افزودن پنل وب، connectorهای دیگر و کنترل چند Node را بدون بازنویسی هسته فراهم می‌کند.

## ساختار پیشنهادی دایرکتوری

```text
src/nexus_ai_agent/
├── core/                         # قراردادهای مستقل از provider
│   ├── domain/                   # value objects و قوانین دامنه
│   ├── ports/                    # Protocolهای ورودی/خروجی
│   ├── services/                 # use-caseهای اصلی
│   └── errors.py                 # خطاهای دامنه و قابل retry
├── connectors/                   # adapterهای بیرونی
│   ├── base.py                   # Connector protocol و Factory
│   ├── telegram/                 # update handlers و Telegram adapter
│   └── registry.py               # ثبت و ساخت connectorها
├── orchestrator/                 # انتخاب strategy و اجرای jobها
│   ├── strategies.py             # Strategyهای orchestration
│   ├── dispatcher.py             # تحویل job به worker
│   └── policies.py               # retry، timeout و circuit-breaker
├── memory/                       # حافظه کوتاه‌مدت و بلندمدت
│   ├── service.py
│   ├── repositories.py
│   └── schemas.py
├── storage/                      # persistence و migration
│   ├── db.py
│   ├── models.py                 # مدل‌های فعلی محصول
│   ├── control_plane_models.py   # Node/Job/Session/Log
│   └── repositories/
├── config/                       # settings و validation محیط
├── observability/                # logging، metrics و health probes
├── workers/                      # workerها و task handlers
└── api/                          # REST/WebSocket در فاز دوم
```

در وضعیت فعلی مخزن، `bot/` و `features/` حفظ می‌شوند تا migration تدریجی ممکن باشد. در گام‌های بعدی، handlerهای Telegram به `connectors/telegram/` منتقل می‌شوند و منطق قابل استفاده مجدد در `core/services/` قرار می‌گیرد. تا زمان تکمیل migration نباید importهای گسترده و پرریسک یک‌باره انجام شود.

## مرز وابستگی‌ها

| لایه | مسئولیت | مجاز به وابستگی به |
|---|---|---|
| Core | use-case، قرارداد و قوانین دامنه | استاندارد Python و typeها |
| Orchestrator | strategy، dispatch و policy | Core و portها |
| Memory/Storage | repository و persistence | Core و SQLModel/SQLAlchemy |
| Connectors | Telegram و سرویس‌های بیرونی | Core ports و SDK مربوطه |
| Workers | اجرای asynchronous job و retry | Orchestrator، Storage و broker adapter |
| API | REST/WebSocket و auth | Core services و repositoryها |
| Observability | log، health و metrics | قراردادهای Core و backend لاگ |

> قانون اصلی: هیچ کد دامنه‌ای نباید مستقیماً Telegram، Redis، PostgreSQL یا یک provider هوش مصنوعی را import کند.

## مدل کنترل‌پلین

`Node` یک نصب مستقل NEXUS روی VPS است. `Job` یک کار قابل اجراست که می‌تواند به Node تخصیص یابد. `AgentSession` وضعیت مکالمه و context را نگه می‌دارد و از provider مستقل است. `SystemLog` رخدادهای عملیاتی، خطاها و سیگنال‌های self-healing را ثبت می‌کند. در فاز دوم، API کنترل‌پلین برای ثبت token‌شده Node، دریافت heartbeat و مشاهده jobها روی این مدل‌ها ساخته می‌شود.

## تصمیم‌های مهم

برای حفظ قابلیت اجرای محلی و تست‌پذیری، مدل‌های جدید با SQLModel و فیلدهای قابل استفاده در SQLite و PostgreSQL طراحی شده‌اند. شناسه‌ها و زمان‌ها صریح هستند، وضعیت‌ها با `str, Enum` محدود می‌شوند، payloadها به‌صورت JSON متن ذخیره می‌شوند تا migration اولیه به provider خاص وابسته نباشد، و indexهای عملیاتی روی status، heartbeat، priority و زمان ایجاد قرار می‌گیرند.

این طراحی هنوز کنترل‌پلین را به اینترنت یا provider پولی وابسته نمی‌کند. PostgreSQL و Redis در گام‌های ۳ و ۴ به‌عنوان زیرساخت قابل جایگزینی اضافه می‌شوند و برای توسعه محلی، SQLite فعلی همچنان قابل استفاده باقی می‌ماند.
