/** Android build served by the marketing site itself (same origin).
 *
 * Drop the file into /var/www/minitv/web/download/ on the server; Nginx serves
 * /download/ as plain files (a missing APK is a real 404, not the SPA page).
 * The filename carries the version so a rebuilt APK is never served from a
 * warm CDN edge; bump all three fields together when a new build ships.
 */
export const ANDROID_APK = {
  url: "/download/asiatv-1.0.0-arm64-v8a.apk",
  version: "1.0.0",
  size: "20 MB",
};

/** Minimum Android the current Flutter toolchain targets (minSdk 24). */
export const ANDROID_MIN = "7.0";
