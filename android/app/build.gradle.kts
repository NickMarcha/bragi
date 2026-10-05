plugins { id("com.android.application"); id("org.jetbrains.kotlin.android") }
android {
    namespace = "com.nickmarcha.bragi"
    compileSdk = 35
    defaultConfig {
        applicationId = "com.nickmarcha.bragi"
        minSdk = 29
        targetSdk = 35
        versionCode = 10
        versionName = "0.1.9"
        buildConfigField("String", "DEFAULT_SERVER", "\"https://sagepi.tail08dfa.ts.net/\"")
    }
    buildFeatures { buildConfig = true }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    signingConfigs {
        create("githubRelease") {
            System.getenv("BRAGI_ANDROID_KEYSTORE")?.let {
                storeFile = file(it)
                storePassword = System.getenv("BRAGI_ANDROID_STORE_PASSWORD")
                keyAlias = System.getenv("BRAGI_ANDROID_KEY_ALIAS")
                keyPassword = System.getenv("BRAGI_ANDROID_KEY_PASSWORD")
            }
        }
    }
    buildTypes {
        debug {
            // Pairs with dev/compose.yml; src/debug allows cleartext to the emulator's host alias only.
            buildConfigField("String", "DEFAULT_SERVER", "\"http://10.0.2.2:20080/\"")
        }
        release {
            isMinifyEnabled = false
            if (System.getenv("BRAGI_ANDROID_KEYSTORE") != null) signingConfig = signingConfigs.getByName("githubRelease")
        }
    }
    testOptions { unitTests.isReturnDefaultValues = true }
}
dependencies {
    implementation("androidx.activity:activity-ktx:1.10.1")
    implementation("androidx.core:core-ktx:1.16.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.10.2")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("org.roc-streaming.roctoolkit:roc-android:0.2.1")
    testImplementation("junit:junit:4.13.2")
    testImplementation("com.squareup.okhttp3:mockwebserver:4.12.0")
    testImplementation("org.json:json:20250107")
}
