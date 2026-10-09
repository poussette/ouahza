[app]
title = Ouahza
package.name = ouahza
package.domain = org.ouahza
source.dir = .
source.include_exts = py
version = 0.1.22
requirements = python3,kivy,requests,certifi,urllib3,idna,charset_normalizer
orientation = portrait
fullscreen = 0

android.permissions = INTERNET,REQUEST_INSTALL_PACKAGES

# Security: keep the app's private data (wallet list, API keys) out of
# Android cloud/adb backups.
android.allow_backup = False
android.api = 33
android.minapi = 21
# Both ABIs on purpose: arm64-v8a for modern phones, armeabi-v7a for older
# and some low-end devices that still run a 32-bit system. Maximum
# compatibility, at the cost of a bigger APK.
android.archs = arm64-v8a,armeabi-v7a
android.accept_sdk_license = True
# Avoids a slow/flaky first-time NDK download picking an unexpected version.
android.ndk = 25b

[buildozer]
log_level = 2
warn_on_root = 0
