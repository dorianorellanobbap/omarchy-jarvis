import QtQuick
import qs.Commons

// A small scrolling window onto command output.
//
// Setup and the microphone test both run a real program and both have
// something to say while they run: what is being downloaded, or when to start
// talking. Hiding that behind a spinner would make a slow step look like a
// hang, and the microphone test would be unusable, since its prompts are the
// whole interface.
//
// It follows the end of the output the way a terminal does, until the reader
// scrolls back, and then it leaves them where they are.
Flickable {
  id: root

  property string text: ""
  property color foreground: Color.foreground
  property string fontFamily: Style.font.family
  property int maxHeight: 180

  // Whether the view is pinned to the end. A reader who scrolls up is asking
  // to stay there, so new lines stop dragging the view away from them.
  readonly property bool atEnd: contentHeight <= height
    || contentY >= contentHeight - height - 4

  implicitHeight: Math.min(body.implicitHeight, maxHeight)
  contentWidth: width
  contentHeight: body.implicitHeight
  clip: true
  boundsBehavior: Flickable.StopAtBounds
  interactive: contentHeight > height

  Text {
    id: body
    width: root.width
    text: root.text
    color: Qt.darker(root.foreground, 1.2)
    font.family: "monospace"
    font.pixelSize: Style.font.caption
    wrapMode: Text.Wrap
    textFormat: Text.PlainText
  }

  onContentHeightChanged: if (root.atEnd) root.scrollToEnd()

  function scrollToEnd() {
    contentY = Math.max(0, contentHeight - height)
  }
}
