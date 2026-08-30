import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Real server URL/credentials live in secrets.properties (gitignored, never
// committed) instead of hardcoded in Kotlin — see secrets.properties.example
// for the format. Falls back to obviously-placeholder values so a fresh
// checkout without that file still compiles, just can't reach a real server.
val secrets = Properties().apply {
    val file = rootProject.file("secrets.properties")
    if (file.exists()) file.inputStream().use { load(it) }
}
fun secret(key: String, fallback: String) = secrets.getProperty(key, fallback)

android {
    namespace = "com.cookbook.app"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.cookbook.app"
        minSdk = 24
        targetSdk = 34
        versionCode = 1
        versionName = "1.0"

        buildConfigField("String", "COOKBOOK_BASE_URL", "\"${secret("cookbookBaseUrl", "http://localhost:8092")}\"")
        buildConfigField("String", "COOKBOOK_USERNAME", "\"${secret("cookbookUsername", "admin")}\"")
        buildConfigField("String", "COOKBOOK_PASSWORD", "\"${secret("cookbookPassword", "changeme")}\"")
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    buildFeatures {
        viewBinding = true
        buildConfig = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.swiperefreshlayout:swiperefreshlayout:1.1.0")
}
