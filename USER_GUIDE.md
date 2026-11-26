# Meeting Captions — User Guide

*Available in: **English** | [Italiano](USER_GUIDE_IT.md)*

**Meeting Captions** listens to your meetings, shows live captions on your screen, and automatically writes clean meeting notes and action items in **Odoo**. Everything runs privately on the office server.

---

## What it does for you

* **Live captions on your screen:** See what everyone is saying in real time in a small box in the corner of your screen (or in a full browser window).
* **Works in person and on calls:** Captures your microphone, and can also caption remote participants on Teams, Zoom, or Google Meet.
* **Automatic meeting notes:** When the meeting ends, it summarizes the discussion, lists key decisions, and extracts action items with owners and deadlines.
* **Saves straight to Odoo:**
  * Full meeting notes are published in **Odoo → Knowledge → Meeting Notes** so everyone can read them.
  * Each action item is created as an assigned task in **Odoo Projects** for the right person.

---

## First-time setup (takes 1 minute)

1. Open **Meeting Captions** from your Windows Start menu (it also starts automatically when you sign in to Windows).
2. Fill in the setup window:
   * **Your name:** Type your name as it appears in Odoo (for example, your full name or last name) so your tasks are assigned to you.
   * **Meeting:** Leave the automatic date & time name, or pick/type the shared meeting name if others are joining the same meeting.
   * **Language you speak:** **Italian** is selected by default. Change it if the meeting is in English or another language.
   * **Microphone:** Choose your headset or desk microphone, or leave it on **System default microphone**.
   * **Also caption the other side of calls:** Turn this on **only** if you are on a video call (Teams, Zoom, Meet). If several people are sitting in the same room on the same call, only **one** person should turn this on.
3. Click **Start**.

---

## Using it during a meeting

### The caption box
A small, semi-transparent box appears in the bottom-right corner of your screen:
* **Drag it anywhere** on your screen by clicking and dragging it.
* **Pause / Resume:** Temporarily stops sending your microphone audio (useful if you step away or take a private call).
* **Hide:** Hides the box from your screen while keeping captions running in the background.
* **End meeting:** Opens the confirmation window to stop and generate your meeting notes.

### The taskbar (system tray) icon
Look for the **Meeting Captions** icon near your Windows clock (bottom-right). Its colored dot tells you the status at a glance:
* 🟢 **Green:** Live and listening
* 🟡 **Yellow:** Paused
* ⚪ **Grey:** Stopped
* 🔴 **Red:** Reconnecting to the office server

Right-click the icon anytime to:
* **Show / Hide captions**
* **Pause / Resume** or **Start / Stop streaming**
* **Change meeting…** — Pick a scheduled meeting from today's Odoo calendar (or type a meeting name) and choose the meeting language.
* **Open live captions** — Opens a larger, easy-to-read live transcript in your web browser.
* **Open dashboard** — Opens the web page to browse past meetings, transcripts, and notes.
* **End meeting & write notes…** — Stops the meeting and generates the notes.

---

## Ending a meeting & reading your notes

When you click **End meeting** (or **Quit** while a meeting is running), a window asks:

> *"Do you want to stop the tool and generate meeting notes?"*

* **Stop & generate** — Stops listening and creates the meeting notes (usually takes 1–2 minutes). When finished, an **Open notes** button appears right on the caption box.
* **Stop only** — Stops listening without creating notes.
* **Cancel** — Keeps listening.

> **Automatic reminders:** If everyone else leaves the meeting, nobody speaks for 10 minutes, or a scheduled Odoo calendar meeting reaches its end time, the app will automatically pop up this same question so you never forget to finish a session.

### Where to find your notes
* **Odoo Knowledge:** Open **Knowledge → Meeting Notes** in Odoo to read the full summary, discussion topics, decisions, and action items table.
* **Odoo Projects:** Action items are automatically assigned to each owner in Odoo.
* **Hub Dashboard:** Every meeting's transcript, notes, and audio recording are also kept safely on the office dashboard.

---

## Quick tips & troubleshooting

* **No captions appearing?** Check that the tray icon dot is **green** (not paused or grey) and that your microphone is not muted on your headset or in Windows.
* **Someone's name wasn't matched in Odoo?** Use your full name or last name in the setup window (for example, if two colleagues share the same first name).
* **Odoo was offline when the meeting ended?** Nothing is lost—the notes are saved on the dashboard, and an admin can click **Publish to Odoo** later with one click.
