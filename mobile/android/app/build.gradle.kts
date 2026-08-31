plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.kotlin.serialization)
}

android {
    namespace = "com.romerez.lsbmobile"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.romerez.lsbmobile"
        minSdk = 34 // S24 Ultra ships API 34; no older devices targeted
        targetSdk = 35
        // BUMP versionCode ON EVERY RELEASE — the in-app updater compares it
        // against the manifest's `app.version_code` (served by the desktop);
        // an unbumped build is invisible to phones as an update.
        versionCode = 3
        versionName = "0.3.0"
        ndk { abiFilters += "arm64-v8a" } // one personal device; keeps later yt-dlp bundle small
    }

    buildTypes {
        release {
            isMinifyEnabled = false // personal sideload; readable stack traces > size
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        compose = true
        buildConfig = true // WifiPuller reads BuildConfig.VERSION_CODE
    }
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.activity.compose)
    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.material3)
    implementation(libs.androidx.compose.ui.tooling.preview)
    debugImplementation(libs.androidx.compose.ui.tooling)
    implementation(libs.androidx.navigation.compose)
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.androidx.media3.exoplayer)
    implementation(libs.androidx.datastore.preferences)
    implementation(libs.kotlinx.serialization.json)
    implementation(libs.kotlinx.coroutines.android)
    implementation(libs.coil.compose)
    implementation(libs.okhttp)
    implementation(libs.play.services.code.scanner)
    // yt-dlp on Android (Seal-maintained fork; bundles a Python runtime,
    // ~+28MB with the arm64-only abiFilter). NO ffmpeg artifact on purpose:
    // we keep m4a/webm as delivered (mobile/README.md §7.7).
    implementation(libs.youtubedl.library)
}
