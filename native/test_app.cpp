// Minimal native Qt application for exercising the qatrec event filter.
//
// Mirrors spike/sample_app.py: named widgets, an unnamed pair distinguishable
// only by text, two identical unnamed buttons, and a password field.
//
// Also provides the discrimination case. Passing --programmatic makes the app
// click its own button through QAbstractButton::click(), which produces a
// NON-spontaneous event. A correct filter must ignore it. Passing nothing and
// clicking with a real pointer produces a spontaneous event, which must be
// captured. That difference is the entire justification for this library.

#include <QtCore/QTimer>
#include <QtGui/QAction>
#include <QtGui/QKeySequence>
#include <QtWidgets/QApplication>
#include <QtWidgets/QCheckBox>
#include <QtWidgets/QGroupBox>
#include <QtWidgets/QHBoxLayout>
#include <QtWidgets/QLabel>
#include <QtWidgets/QLineEdit>
#include <QtWidgets/QMainWindow>
#include <QtWidgets/QPushButton>
#include <QtWidgets/QVBoxLayout>
#include <QtWidgets/QWidget>

int main(int argc, char **argv)
{
    QApplication app(argc, argv);
    app.setApplicationName("QatRecTestApp");

    QMainWindow window;
    window.setObjectName("mainWindow");

    auto *root = new QWidget;
    root->setObjectName("rootWidget");
    auto *layout = new QVBoxLayout(root);

    auto *credentials = new QGroupBox("Credentials");
    credentials->setObjectName("credentialsGroup");
    auto *form = new QVBoxLayout(credentials);

    auto *username = new QLineEdit;
    username->setObjectName("usernameField");
    form->addWidget(username);

    auto *password = new QLineEdit;
    password->setObjectName("passwordField");
    password->setEchoMode(QLineEdit::Password);
    form->addWidget(password);

    auto *remember = new QCheckBox("Remember me");
    remember->setObjectName("rememberBox");
    form->addWidget(remember);

    layout->addWidget(credentials);

    // Unnamed, distinguishable by text.
    auto *row = new QHBoxLayout;
    row->addWidget(new QPushButton("Import"));
    row->addWidget(new QPushButton("Export"));
    layout->addLayout(row);

    // Unnamed and identical.
    auto *duplicates = new QGroupBox("Duplicate controls");
    duplicates->setObjectName("duplicateGroup");
    auto *duplicateRow = new QHBoxLayout(duplicates);
    duplicateRow->addWidget(new QPushButton("Apply"));
    duplicateRow->addWidget(new QPushButton("Apply"));
    layout->addWidget(duplicates);

    auto *login = new QPushButton("Sign in");
    login->setObjectName("loginButton");
    layout->addWidget(login);

    auto *status = new QLabel("ready");
    status->setObjectName("statusLabel");
    layout->addWidget(status);

    QObject::connect(login, &QPushButton::clicked, [status] {
        status->setText("signed in");
    });

    // A window-scoped shortcut. Qt dispatches these to the window, not to the
    // focused widget, which is what a recorder has to get right.
    auto *save = new QAction("Save", &window);
    save->setObjectName("saveAction");
    save->setShortcut(QKeySequence("Ctrl+S"));
    QObject::connect(save, &QAction::triggered, [status] {
        status->setText("saved");
    });
    window.addAction(save);

    window.setCentralWidget(root);
    window.resize(420, 420);
    window.show();

    const QStringList arguments = app.arguments();

    if (arguments.contains("--programmatic")) {
        // Non-spontaneous by construction: must NOT be recorded.
        QTimer::singleShot(800, [login, remember] {
            login->click();
            remember->setChecked(true);
        });
    }

    if (arguments.contains("--quit-after")) {
        const int index = arguments.indexOf("--quit-after");
        const int ms = (index + 1 < arguments.size())
                           ? arguments.at(index + 1).toInt() : 5000;
        QTimer::singleShot(ms, &app, &QApplication::quit);
    }

    return app.exec();
}
