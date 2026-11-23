import CSI 1.0
import QtQuick 2.0
import Traktor.Gui 1.0 as T

// Experiment: render Traktor's own overview waveform for one deck and save it as a PNG for the Ripped Ripper app.
// Loaded through a Loader (see ApiModule.qml), so if anything here fails to load the rest of the mod keeps working.
T.OffscreenView {
  id: view
  property int deckId: 0
  readonly property string letter: String.fromCharCode(65 + deckId)
  readonly property string outFile: "@WAVE_DIR@/deck" + letter + ".png"
  width: 1200
  height: 80

  AppProperty { path: "app.traktor.decks." + (view.deckId + 1) + ".track.content.file_path"; onValueChanged: grabTimer.restart() }

  T.Stripe {
    id: stripe
    anchors.fill: parent
    deckId: view.deckId
  }

  // wait for Traktor to finish drawing the new track's overview, then grab twice (it fills in while analysing)
  Timer { id: grabTimer; interval: 2500; onTriggered: { view.grab(); secondGrab.restart() } }
  Timer { id: secondGrab; interval: 4000; onTriggered: view.grab() }

  function grab() {
    stripe.grabToImage(function(result) { var ok = result.saveToFile(view.outFile); console.log("ripper waveform deck " + view.letter + " saved=" + ok) })
  }
  Component.onCompleted: console.log("ripper waveform view loaded for deck " + view.letter)
}
