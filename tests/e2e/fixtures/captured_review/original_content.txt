# Board Foot Calculator — Android App Implementation Plan

# 

---

## 0. Prerequisites

Before scaffolding, verify the Android build toolchain is available:

1. **JDK 17** — AGP 8.7.x requires it. Install if missing: `sudo apt install openjdk-17-jdk`
2. **Android SDK** — need `platforms;android-35` and `build-tools;35.0.0`. Install via Android Studio SDK Manager or `sdkmanager` CLI.
3. **Gradle wrapper** — will be bootstrapped from a local Gradle 8.9 install or generated via `gradle wrapper --gradle-version 8.9`.

---

## 1. Version Matrix

| Component   | Version | Notes                                        |
| ----------- | ------- | -------------------------------------------- |
| AGP         | 8.7.3   | Stable patch in the 8.7 line                 |
| Gradle      | 8.9     | Required by AGP 8.7                          |
| Kotlin      | 2.0.21  | Stable, compatible with AGP 8.7 + Gradle 8.9 |
| compileSdk  | 35      | Android 15                                   |
| targetSdk   | 35      | Matches compileSdk                           |
| minSdk      | 33      | Per spec: "Target Android API level 33+"     |
| JDK         | 17      | Required by AGP 8.x                          |
| Build Tools | 35.0.0  | Matches compileSdk                           |

---

## 2. Project Structure (11 authored files)

```
BoardFootCalculator/
├── .gitignore
├── build.gradle.kts                          # Root: plugin versions
├── settings.gradle.kts                       # Repos + module include
├── gradle.properties                         # JVM args, AndroidX flags
├── gradle/wrapper/
│   ├── gradle-wrapper.jar
│   └── gradle-wrapper.properties             # Pins Gradle 8.9
├── gradlew / gradlew.bat
└── app/
    ├── build.gradle.kts                      # Module: SDK, deps, Kotlin
    └── src/main/
        ├── AndroidManifest.xml
        ├── java/com/kelleyb/boardfootcalculator/
        │   └── MainActivity.kt               # All app logic
        └── res/
            ├── layout/activity_main.xml       # Single-screen layout
            └── values/
                ├── strings.xml
                ├── colors.xml
                └── themes.xml
```

---

## 3. File-by-File Breakdown

### 3.1 `settings.gradle.kts`

- `pluginManagement` block with google(), mavenCentral(), gradlePluginPortal()
- `dependencyResolutionManagement` with FAIL_ON_PROJECT_REPOS
- `rootProject.name = "BoardFootCalculator"` + `include(":app")`

### 3.2 `build.gradle.kts` (root)

- Declare `com.android.application` v8.7.3 and `org.jetbrains.kotlin.android` v2.0.21 as plugins, `apply false`

### 3.3 `gradle.properties`

- Standard: `android.useAndroidX=true`, `kotlin.code.style=official`, `android.nonTransitiveRClass=true`, JVM args

### 3.4 `gradle/wrapper/gradle-wrapper.properties`

- Pin `distributionUrl` to `gradle-8.9-bin.zip`

### 3.5 `app/build.gradle.kts`

- `namespace = "com.kelleyb.boardfootcalculator"`
- compileSdk/targetSdk 35, minSdk 33
- JDK 17 source/target compatibility
- Dependencies (standard AndroidX only — not "external"):
  - `androidx.core:core-ktx:1.13.1`
  - `androidx.appcompat:appcompat:1.7.0`
  - `com.google.android.material:material:1.12.0`
  - `androidx.constraintlayout:constraintlayout:2.1.4`

### 3.6 `AndroidManifest.xml`

- Single `<activity>` with MAIN/LAUNCHER intent filter
- `allowBackup="false"` (no persistence)
- Theme: `@style/Theme.BoardFootCalculator`

### 3.7 `strings.xml`

All user-facing text externalized:

- App name, field hints, button labels
- Format strings: `result_format` (`%1$s x %2$s x %3$s = %4$.2f bf | $%5$.2f`), `total_format`, `total_default`
- Toast messages: `toast_enter_dimensions`, `toast_set_price`

### 3.8 `colors.xml`

- Minimal: black + white. Material theme handles the rest.

### 3.9 `themes.xml`

- `Theme.BoardFootCalculator` parent `Theme.Material3.Light.NoActionBar`

### 3.10 `activity_main.xml` — Layout Design

Root: `ScrollView` → vertical `LinearLayout` (24dp padding)

| #   | Element        | Widget                                                                  | Details                                                                      |
| --- | -------------- | ----------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| 1   | Price input    | `TextInputLayout` + `TextInputEditText`                                 | `inputType="numberDecimal"`, outlined style                                  |
| 2   | Dimensions row | Horizontal `LinearLayout` with 3× `TextInputLayout`/`TextInputEditText` | Each `layout_weight="1"`, 8dp spacing, `inputType="numberDecimal"`           |
| 3   | Calculate btn  | `MaterialButton`                                                        | Full width, primary style                                                    |
| 4   | Result text    | `TextView`                                                              | 18sp, center-aligned, monospace                                              |
| 5   | Total text     | `TextView`                                                              | 20sp bold, center-aligned, monospace, default text "Total: 0.00 bf \| $0.00" |
| 6   | Clear All btn  | `MaterialButton` (TonalButton)                                          | Full width, visually secondary                                               |

### 3.11 `MainActivity.kt` — Core Logic

**State:**

- `totalBoardFeet: Double = 0.0`
- `totalCost: Double = 0.0`

**`onCreate`:** `setContentView`, `findViewById` for all views, wire click listeners.

**`calculate()`:**

1. Parse price → if null or 0.0 → toast "Set a price per board foot", return
2. Parse length/width/thickness → if any null or 0.0 → toast "Enter all dimensions", return
3. `boardFeet = (L × W × T) / 144.0`
4. `cost = boardFeet × price`
5. Round both to 2 decimals: `Math.round(x * 100.0) / 100.0`
6. Set `textResult` with formatted string (shows `L x W x T = X.XX bf | $X.XX`)
7. Accumulate rounded values into running totals
8. Update `textTotal`
9. Clear dimension fields, return focus to Length

**`clearAll()`:**

1. Reset `totalBoardFeet` and `totalCost` to 0.0
2. Clear `textResult`, reset `textTotal` to default
3. Clear dimension fields (NOT price)
4. Focus Length field

### 3.12 `.gitignore`

Standard Android: `.gradle/`, `/build`, `/app/build`, `.idea/`, `local.properties`, `*.iml`

---

## 4. Implementation Sequence

| Step | Action                                                                    |
| ---- | ------------------------------------------------------------------------- |
| 1    | Verify JDK 17 and Android SDK are available                               |
| 2    | Create project directory structure under `~/Desktop/BoardFootCalculator/` |
| 3    | Write `settings.gradle.kts`, root `build.gradle.kts`, `gradle.properties` |
| 4    | Generate Gradle wrapper (8.9)                                             |
| 5    | Write `app/build.gradle.kts`                                              |
| 6    | Write `AndroidManifest.xml`                                               |
| 7    | Write resource files: `strings.xml`, `colors.xml`, `themes.xml`           |
| 8    | Write `activity_main.xml`                                                 |
| 9    | Write `MainActivity.kt`                                                   |
| 10   | Write `.gitignore`                                                        |
| 11   | Run `./gradlew assembleDebug` to verify build                             |
| 12   | Initialize git repo + initial commit                                      |

---

## 5. Verification

### Build check

```bash
cd ~/Desktop/BoardFootCalculator && ./gradlew assembleDebug
```

Expected: `BUILD SUCCESSFUL`, APK at `app/build/outputs/apk/debug/app-debug.apk`

### Manual test matrix

| Test Case              | Input                            | Expected                                    |
| ---------------------- | -------------------------------- | ------------------------------------------- |
| No price               | Price empty, dims set            | Toast: "Set a price per board foot"         |
| Zero price             | Price=0, dims set                | Toast: "Set a price per board foot"         |
| No dimensions          | Price=5, dims empty              | Toast: "Enter all dimensions"               |
| Zero dimension         | Price=5, L=0 W=6 T=1             | Toast: "Enter all dimensions"               |
| 1 board foot reference | Price=10, L=12 W=12 T=1          | `12 x 12 x 1 = 1.00 bf \| $10.00`           |
| Single calc            | Price=5, L=12 W=6 T=1            | `12 x 6 x 1 = 0.50 bf \| $2.50`             |
| Running total          | Two calcs above                  | Total: `1.50 bf \| $12.50`                  |
| Decimal inputs         | Price=8.50, L=10.5 W=5.25 T=1.75 | ~0.67 bf, ~$5.70                            |
| Clear All              | After calcs → Clear All          | Result blank, total zeroed, price unchanged |

---

## 6. Design Decisions

- **No ViewModel/LiveData** — two doubles of state don't warrant architecture components
- **No ViewBinding** — `findViewById` is sufficient for 8 views in one Activity
- **No `onSaveInstanceState`** — spec says no persistence; rotation resets totals (acceptable)
- **No unit tests** — trivially verifiable formula; can add later if desired
- **No custom launcher icon** — uses default; can customize later
- **Dimensions auto-clear after Calculate** — supports rapid multi-piece entry workflow
- **Price checked before dimensions** — validation order matches spec toast priority
- **Running total accumulates rounded values** — prevents visible floating-point drift between individual results and totals
