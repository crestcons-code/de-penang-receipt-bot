/**
 * Weekly Google Drive backup of the DE Penang dana list.
 *
 * Runs on Google's servers, not on anyone's PC, so it happens whether or not
 * the office laptop is switched on. It runs as YOU, so each copy is owned by
 * your Google account and uses your own storage - which is why this works
 * where the app's service account cannot (service accounts have no Drive
 * storage quota on a personal Google account).
 *
 * SETUP (once):
 *   1. Go to https://script.google.com  ->  New project
 *   2. Delete the sample code, paste this whole file in, and Save
 *   3. Put your spreadsheet's ID in SHEET_ID below (see the comment there)
 *   4. In the function dropdown at the top, choose  createWeeklyTrigger
 *      and click Run. Approve the permission prompt when Google asks.
 *      (The prompt is Google asking whether this script may touch your Drive.)
 *   5. Choose  backupDanaList  and click Run once, to prove it works.
 *      A folder named below should appear in your Drive with one copy in it.
 *
 * After that it runs by itself every Sunday morning.
 */

// The long code in your dana list's URL, between /d/ and /edit :
//   https://docs.google.com/spreadsheets/d/THIS_PART_HERE/edit
var SHEET_ID = 'PASTE_YOUR_SPREADSHEET_ID_HERE';

var FOLDER_NAME = 'DE Penang Dana Backups';
var KEEP = 12;   // how many weekly copies to retain

/** Makes one dated copy of the whole spreadsheet into the backup folder. */
function backupDanaList() {
  if (SHEET_ID === 'PASTE_YOUR_SPREADSHEET_ID_HERE') {
    throw new Error('Set SHEET_ID first - see the comment above it.');
  }

  var folder = getOrCreateFolder_(FOLDER_NAME);
  var stamp = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd');
  var name = 'dana-backup-' + stamp;

  // If today's copy already exists (a manual re-run), replace it rather than
  // ending up with duplicates for the same date.
  var sameDay = folder.getFilesByName(name);
  while (sameDay.hasNext()) {
    sameDay.next().setTrashed(true);
  }

  DriveApp.getFileById(SHEET_ID).makeCopy(name, folder);
  prune_(folder, KEEP);
}

function getOrCreateFolder_(name) {
  var it = DriveApp.getFoldersByName(name);
  return it.hasNext() ? it.next() : DriveApp.createFolder(name);
}

/** Keeps only the newest `keep` backups; older ones go to Trash (recoverable). */
function prune_(folder, keep) {
  var files = [];
  var it = folder.getFiles();
  while (it.hasNext()) {
    var f = it.next();
    if (f.getName().indexOf('dana-backup-') === 0) {
      files.push(f);
    }
  }
  files.sort(function (a, b) {
    return b.getDateCreated().getTime() - a.getDateCreated().getTime();
  });
  files.slice(keep).forEach(function (f) {
    f.setTrashed(true);
  });
}

/** Run this ONCE to schedule the weekly backup (Sunday ~9am). */
function createWeeklyTrigger() {
  // Clear any previous trigger for this function so running setup twice
  // doesn't leave two triggers making two copies.
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'backupDanaList') {
      ScriptApp.deleteTrigger(t);
    }
  });

  ScriptApp.newTrigger('backupDanaList')
    .timeBased()
    .onWeekDay(ScriptApp.WeekDay.SUNDAY)
    .atHour(9)
    .create();

  Logger.log('Weekly backup scheduled for Sunday mornings.');
}
