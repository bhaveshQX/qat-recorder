/*
 * qatembedded.cpp -- a Qat plugin for QML that is embedded in a widget.
 *
 * Qat finds an object by searching down from the top-level windows its plugins
 * report. A QQuickView put inside a widget with QWidget::createWindowContainer
 * is not one: its parent is the main window's QWidgetWindow, which no widget
 * search goes through, so Qat has no path to anything in it. Qat's own QML
 * plugin starts from qApp->allWindows(), which does include it -- but fills its
 * array by the index into that list, so a QML window that comes after a widget
 * window is counted and then never written.
 *
 * Observed on mako_shoulder, whose whole interface is a VIS::QuickView_C inside
 * "STP Main Window": recording worked, because the event filter reads objects
 * from inside the application, and every replay failed with
 *
 *     {'objectName': 'roundButton'} -- no object in the application has this
 *
 * This plugin reports those windows and nothing else. Wrapping, grabbing and
 * picking are left to Qat's QML plugin, which handles a QQuickWindow however it
 * was reached. A top-level QML window is not reported here, so an ordinary QML
 * application sees no difference.
 *
 * Qat loads every library in its plugins folder whose name ends in the Qt
 * version its server was built for -- `build-filter` installs this one under
 * each of them. Public QtCore and QtGui API only, like the filter, so one build
 * serves every later minor of the same major.
 */

#include <QtGui/QGuiApplication>
#include <QtGui/QWindow>

#define QAT_EXPORT extern "C" __attribute__((visibility("default")))

namespace {

QList<QWindow *> embeddedQuickWindows()
{
    QList<QWindow *> found;
    for (QWindow *window : QGuiApplication::allWindows()) {
        if (window->parent() && window->inherits("QQuickWindow"))
            found.push_back(window);
    }
    return found;
}

}  // namespace

/* The protocol of Qat's own plugins: called with *size == 0 to ask how many,
 * then again with an array of that size to fill. */
QAT_EXPORT bool GetTopWindows(QObject **windows, unsigned int *size)
{
    if (!size)
        return false;
    const QList<QWindow *> found = embeddedQuickWindows();
    const auto count = static_cast<unsigned int>(found.size());
    if (*size == 0) {
        *size = count;
        return true;
    }
    if (!windows)
        return false;
    for (unsigned int i = 0; i < *size && i < count; ++i)
        windows[i] = found[static_cast<int>(i)];
    return true;
}

/* Qat's QML plugin does these for any QQuickWindow; null means "not mine". */
QAT_EXPORT void *CastObject(const QObject *) { return nullptr; }
QAT_EXPORT void *GrabImage(QObject *) { return nullptr; }
QAT_EXPORT void *CreatePicker(QObject *) { return nullptr; }
