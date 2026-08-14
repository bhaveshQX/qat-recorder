// -*- coding: utf-8 -*-
//
// qatrec — a global Qt event filter for recording real user input.
//
// Loaded with LD_PRELOAD alongside Qat's own injector. It does not touch Qat's
// source or its protocol: Qat keeps doing introspection, naming and playback,
// while this library answers the one question Qat cannot — "did a human do that?"
//
// The discriminator is QEvent::spontaneous(), which is true only for events that
// came from the window system. Programmatic changes and Qat's own synthesised
// playback events are both non-spontaneous, so both are ignored automatically.
// Nothing else can make that distinction from outside the process.
//
// Activation is opt-in: with QATREC_PORT unset the filter is never installed and
// the library is inert, so it is safe to preload unconditionally.
//
// Public Qt API only. That is a hard rule: it is what keeps one build binary
// compatible across a whole Qt major version.

#include <QtCore/QCoreApplication>
#include <QtCore/QDateTime>
#include <QtCore/QEvent>
#include <QtCore/QMetaObject>
#include <QtCore/QObject>
#include <QtCore/QPoint>
#include <QtCore/QString>
#include <QtCore/QVariant>
#include <QtCore/QtGlobal>

#include <QtGui/QKeyEvent>
#include <QtGui/QMouseEvent>
#include <QtGui/QWheelEvent>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <mutex>
#include <string>
#include <thread>

#include <arpa/inet.h>
#include <dlfcn.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

namespace {

constexpr int kMaxQueue = 4096;
constexpr int kMaxAncestors = 12;

// ---------------------------------------------------------------------------
// Outbound queue. A mutex is deliberate rather than lock-free: these events fire
// at human speed (tens per second at most, after the type filter below), so the
// contention is nil and correctness is worth more than the cleverness. What we
// must never do on the GUI thread is socket I/O, and that still happens only on
// the writer thread.
// ---------------------------------------------------------------------------

std::mutex g_mutex;
std::condition_variable g_cv;
std::deque<std::string> g_queue;
std::atomic<bool> g_running{false};
std::atomic<unsigned long> g_dropped{0};
std::atomic<unsigned long> g_sent{0};

void enqueue(std::string &&line)
{
    {
        std::lock_guard<std::mutex> lock(g_mutex);
        if (g_queue.size() >= kMaxQueue) {
            g_dropped.fetch_add(1, std::memory_order_relaxed);
            return;
        }
        g_queue.push_back(std::move(line));
    }
    g_cv.notify_one();
}

// ---------------------------------------------------------------------------
// JSON helpers. Small and dependency-free on purpose.
// ---------------------------------------------------------------------------

void appendEscaped(std::string &out, const std::string &value)
{
    for (char c : value) {
        switch (c) {
        case '"':  out += "\\\""; break;
        case '\\': out += "\\\\"; break;
        case '\n': out += "\\n";  break;
        case '\r': out += "\\r";  break;
        case '\t': out += "\\t";  break;
        default:
            if (static_cast<unsigned char>(c) < 0x20) {
                char buf[8];
                std::snprintf(buf, sizeof(buf), "\\u%04x", c & 0xff);
                out += buf;
            } else {
                out += c;
            }
        }
    }
}

void appendString(std::string &out, const char *key, const std::string &value)
{
    out += '"';
    out += key;
    out += "\":\"";
    appendEscaped(out, value);
    out += "\",";
}

void appendInt(std::string &out, const char *key, long long value)
{
    out += '"';
    out += key;
    out += "\":";
    out += std::to_string(value);
    out += ',';
}

std::string toStd(const QString &s)
{
    const QByteArray bytes = s.toUtf8();
    return std::string(bytes.constData(), static_cast<size_t>(bytes.size()));
}

// ---------------------------------------------------------------------------
// Locator: enough structure for the Python side to resolve the object through
// Qat and validate a definition for it.
//
// Built inline, on the GUI thread, on purpose. A QObject* cannot safely be
// dereferenced later from the writer thread — the object may be gone by then.
// The cost is bounded (at most kMaxAncestors levels) and only paid for the few
// event types that survive the switch in eventFilter().
// ---------------------------------------------------------------------------

std::string propertyIfAny(const QObject *object, const char *name)
{
    const QVariant value = object->property(name);
    if (!value.isValid())
        return std::string();
    const QString text = value.toString();
    if (text.isEmpty())
        return std::string();
    return toStd(text.left(200));
}

int siblingIndex(const QObject *object)
{
    const QObject *parent = object->parent();
    if (!parent)
        return -1;
    const char *className = object->metaObject()->className();
    int index = 0;
    const QObjectList &siblings = parent->children();
    for (const QObject *sibling : siblings) {
        if (sibling == object)
            return index;
        if (std::strcmp(sibling->metaObject()->className(), className) == 0)
            ++index;
    }
    return -1;
}

// ---------------------------------------------------------------------------
// Which menu item was clicked
//
// A menu item is a QAction, not a widget. QMenu and QMenuBar draw their items
// themselves, so the event filter only ever sees the menu -- and the menu is
// the wrong thing to click on replay, because Qat aims at the centre of a
// widget and the centre of a full-width menu bar is empty space. The item has
// to be asked for by position, and the only API that answers is
// QMenu::actionAt(), which lives in QtWidgets.
//
// Linking QtWidgets is not an option: this library is preloaded into QML
// applications too, and a hard dependency on a library they never load would
// stop them starting at all. So the symbols are looked up in whatever the
// process already has mapped. On a widgets application they resolve; on a QML
// application they are absent and this whole feature switches itself off --
// which is right, because in QML a menu item IS an object and arrives through
// the ordinary path.
//
// Calling a non-virtual member function through a plain function pointer is
// well-defined on the Itanium C++ ABI, which is the ABI of every platform this
// library targets. `this` comes first; QObject is the first base of both
// QWidget and QAction, so the pointers need no adjustment.
// ---------------------------------------------------------------------------

using ActionAtFn = QObject *(*)(const QObject *, const QPoint &);

ActionAtFn lookupActionAt(const char *symbol)
{
    return reinterpret_cast<ActionAtFn>(::dlsym(RTLD_DEFAULT, symbol));
}

void appendMenuItem(std::string &out, QObject *object, const QPoint &position)
{
    static ActionAtFn menuActionAt =
        lookupActionAt("_ZNK5QMenu8actionAtERK6QPoint");
    static ActionAtFn menuBarActionAt =
        lookupActionAt("_ZNK8QMenuBar8actionAtERK6QPoint");

    ActionAtFn actionAt = nullptr;
    if (object->inherits("QMenu"))
        actionAt = menuActionAt;
    else if (object->inherits("QMenuBar"))
        actionAt = menuBarActionAt;
    if (!actionAt)
        return;

    QObject *action = actionAt(object, position);
    if (!action)
        return;         // on the menu, but between items or on a separator

    const std::string text = propertyIfAny(action, "text");
    if (!text.empty())
        appendString(out, "menuItem", text);
    const std::string name = toStd(action->objectName());
    if (!name.empty())
        appendString(out, "menuItemName", name);
}

void appendLocator(std::string &out, QObject *object,
                   const QPoint *position = nullptr)
{
    out += "\"target\":{";
    appendString(out, "class", object->metaObject()->className());
    appendString(out, "objectName", toStd(object->objectName()));

    const std::string text = propertyIfAny(object, "text");
    if (!text.empty())
        appendString(out, "text", text);
    const std::string title = propertyIfAny(object, "title");
    if (!title.empty())
        appendString(out, "title", title);

    if (position)
        appendMenuItem(out, object, *position);

    appendInt(out, "index", siblingIndex(object));

    out += "\"path\":[";
    QObject *current = object->parent();
    int depth = 0;
    bool first = true;
    while (current && depth < kMaxAncestors) {
        if (!first)
            out += ',';
        first = false;
        out += "{\"class\":\"";
        appendEscaped(out, current->metaObject()->className());
        out += "\",\"objectName\":\"";
        appendEscaped(out, toStd(current->objectName()));
        out += "\"}";
        current = current->parent();
        ++depth;
    }
    out += "]}";
}

// ---------------------------------------------------------------------------
// The filter itself
// ---------------------------------------------------------------------------

const char *kindFor(QEvent::Type type)
{
    switch (type) {
    case QEvent::MouseButtonPress:   return "mouse_press";
    case QEvent::MouseButtonRelease: return "mouse_release";
    case QEvent::MouseButtonDblClick:return "mouse_double";
    case QEvent::KeyPress:           return "key_press";
    case QEvent::KeyRelease:         return "key_release";
    case QEvent::Wheel:              return "wheel";
    case QEvent::FocusIn:            return "focus_in";
    default:                         return nullptr;
    }
}

//: When real window-system input last arrived. QEvent::Shortcut is synthesised
//: by Qt's shortcut machinery rather than delivered by the window system, so
//: spontaneous() is false for it and the usual gate would drop every shortcut.
//: Correlating against recent genuine input keeps user-pressed shortcuts and
//: still rejects a programmatic action->trigger() in an otherwise idle app.
std::atomic<long long> g_lastSpontaneous{0};

constexpr long long kShortcutWindowMs = 250;

bool tracksRealInput(QEvent::Type type)
{
    switch (type) {
    case QEvent::MouseButtonPress:
    case QEvent::MouseButtonRelease:
    case QEvent::MouseButtonDblClick:
    case QEvent::KeyPress:
    case QEvent::KeyRelease:
    case QEvent::ShortcutOverride:   // sent before the shortcut machinery runs
    case QEvent::Wheel:
        return true;
    default:
        return false;
    }
}

class QatRecFilter : public QObject
{
public:
    explicit QatRecFilter(QObject *parent = nullptr) : QObject(parent) {}

protected:
    bool eventFilter(QObject *object, QEvent *event) override
    {
        const QEvent::Type type = event->type();
        const long long now = QDateTime::currentMSecsSinceEpoch();

        // Remember when the window system last gave us something, including
        // events that are never recorded themselves.
        if (event->spontaneous() && tracksRealInput(type))
            g_lastSpontaneous.store(now, std::memory_order_relaxed);

        // Shortcuts bound to a QAction never reach the focused widget as a key
        // press -- Qt consumes them and sends QEvent::Shortcut instead, which is
        // not spontaneous. Without this branch every application shortcut is
        // silently missing from recordings.
        if (type == QEvent::Shortcut) {
            if (!object)
                return false;
            if (now - g_lastSpontaneous.load(std::memory_order_relaxed)
                    > kShortcutWindowMs)
                return false;           // triggered from code, not by a person
            auto *shortcut = static_cast<QShortcutEvent *>(event);
            std::string line;
            line.reserve(512);
            line += '{';
            appendString(line, "kind", "shortcut");
            appendInt(line, "t", now);
            appendString(line, "keys", toStd(shortcut->key().toString()));
            appendLocator(line, object);
            line += '}';
            line += '\n';
            enqueue(std::move(line));
            return false;
        }

        // Reject on type first. Every event in the application passes through
        // here, including paints and timers, so this switch is the hot path and
        // must stay a jump table over an enum.
        const char *kind = kindFor(type);
        if (!kind)
            return false;

        // The whole point: only events originating from the window system are
        // user input. Qat's own playback and any programmatic change are not
        // spontaneous and are dropped here.
        if (!event->spontaneous())
            return false;

        if (!object)
            return false;

        std::string line;
        line.reserve(512);
        line += '{';
        appendString(line, "kind", kind);
        appendInt(line, "t", now);

        // Where the pointer was, in the receiving widget's coordinates. Used to
        // ask a menu which of its items was hit; never recorded as a coordinate
        // to replay, because clicking by position is exactly what makes a
        // recorded script break the first time a layout changes.
        QPoint mousePosition;
        const QPoint *hitPoint = nullptr;

        switch (type) {
        case QEvent::MouseButtonPress:
        case QEvent::MouseButtonRelease:
        case QEvent::MouseButtonDblClick: {
            auto *mouse = static_cast<QMouseEvent *>(event);
            appendInt(line, "button", static_cast<long long>(mouse->button()));
            appendInt(line, "modifiers", static_cast<long long>(mouse->modifiers()));
#if QT_VERSION >= QT_VERSION_CHECK(6, 0, 0)
            mousePosition = mouse->position().toPoint();
#else
            mousePosition = mouse->pos();
#endif
            appendInt(line, "x", static_cast<long long>(mousePosition.x()));
            appendInt(line, "y", static_cast<long long>(mousePosition.y()));
            hitPoint = &mousePosition;
            break;
        }
        case QEvent::KeyPress:
        case QEvent::KeyRelease: {
            auto *key = static_cast<QKeyEvent *>(event);
            appendInt(line, "key", key->key());
            appendInt(line, "modifiers", static_cast<long long>(key->modifiers()));
            // Deliberately NOT recording key->text(): typed characters are
            // reconstructed on the Python side, where the target's echoMode is
            // known and password fields can be redacted before anything is
            // written to disk.
            break;
        }
        case QEvent::Wheel: {
            auto *wheel = static_cast<QWheelEvent *>(event);
            appendInt(line, "dx", wheel->angleDelta().x());
            appendInt(line, "dy", wheel->angleDelta().y());
            break;
        }
        default:
            break;
        }

        appendLocator(line, object, hitPoint);
        line += '}';
        line += '\n';

        enqueue(std::move(line));
        return false;   // never consume: the application must behave normally
    }
};

// ---------------------------------------------------------------------------
// Writer thread
// ---------------------------------------------------------------------------

int connectTo(int port)
{
    const int fd = ::socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0)
        return -1;
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_port = htons(static_cast<uint16_t>(port));
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (::connect(fd, reinterpret_cast<sockaddr *>(&address), sizeof(address)) != 0) {
        ::close(fd);
        return -1;
    }
    return fd;
}

void writerLoop(int port)
{
    int fd = -1;
    for (int attempt = 0; attempt < 50 && fd < 0 && g_running.load(); ++attempt) {
        fd = connectTo(port);
        if (fd < 0)
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    if (fd < 0) {
        qWarning("qatrec: could not connect to recorder on port %d", port);
        return;
    }

    while (g_running.load()) {
        std::string line;
        {
            std::unique_lock<std::mutex> lock(g_mutex);
            g_cv.wait_for(lock, std::chrono::milliseconds(200),
                          [] { return !g_queue.empty() || !g_running.load(); });
            if (g_queue.empty())
                continue;
            line = std::move(g_queue.front());
            g_queue.pop_front();
        }
        size_t offset = 0;
        while (offset < line.size()) {
            const ssize_t written = ::send(fd, line.data() + offset,
                                           line.size() - offset, MSG_NOSIGNAL);
            if (written <= 0) {
                ::close(fd);
                return;
            }
            offset += static_cast<size_t>(written);
        }
        g_sent.fetch_add(1, std::memory_order_relaxed);
    }
    ::close(fd);
}

// ---------------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------------

void qatrecStartup()
{
    const char *portText = std::getenv("QATREC_PORT");
    if (!portText || !*portText)
        return;                      // inert unless explicitly asked for

    const int port = std::atoi(portText);
    if (port <= 0 || port > 65535)
        return;

    QCoreApplication *app = QCoreApplication::instance();
    if (!app)
        return;

    g_running.store(true);
    std::thread(writerLoop, port).detach();

    // Installed on the application object, so it sees events delivered to every
    // object in this thread.
    app->installEventFilter(new QatRecFilter(app));

    QObject::connect(app, &QCoreApplication::aboutToQuit, app, [] {
        g_running.store(false);
        g_cv.notify_all();
    });

    qInfo("qatrec: recording to port %d", port);
}

}  // namespace

Q_COREAPP_STARTUP_FUNCTION(qatrecStartup)

// Exposed for tests: how many records were produced and dropped.
extern "C" unsigned long qatrec_sent()    { return g_sent.load(); }
extern "C" unsigned long qatrec_dropped() { return g_dropped.load(); }
