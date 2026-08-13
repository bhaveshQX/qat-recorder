import QtQuick

Rectangle {
    width: 360; height: 120
    color: "#eef2f1"

    Rectangle {
        objectName: "qmlNamedTile"
        x: 16; y: 20; width: 140; height: 72; radius: 6
        color: ma1.pressed ? "#0e6f60" : "#8fb8ae"
        Text { anchors.centerIn: parent; text: "Named tile" }
        MouseArea { id: ma1; objectName: "qmlNamedArea"; anchors.fill: parent }
    }

    Rectangle {
        x: 180; y: 20; width: 140; height: 72; radius: 6
        color: ma2.pressed ? "#a34c18" : "#d9b9a4"
        Text { anchors.centerIn: parent; text: "Unnamed tile" }
        MouseArea { id: ma2; anchors.fill: parent }
    }
}
