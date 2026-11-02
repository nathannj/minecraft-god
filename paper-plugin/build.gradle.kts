plugins {
    java
}

group = "com.minecraftgod"
version = "0.1.0"

repositories {
    mavenCentral()
    maven("https://repo.papermc.io/repository/maven-public/")
    maven("https://maven.maxhenkel.de/repository/public")
}

dependencies {
    compileOnly("io.papermc.paper:paper-api:26.3.build.231-stable")
    compileOnly("de.maxhenkel.voicechat:voicechat-api:2.6.27")
    testImplementation("io.papermc.paper:paper-api:26.3.build.231-stable")
    testImplementation("de.maxhenkel.voicechat:voicechat-api:2.6.27")
}

java {
    toolchain.languageVersion.set(JavaLanguageVersion.of(25))
}

tasks.processResources {
    filesMatching("plugin.yml") {
        expand("version" to project.version)
    }
}

val outboxTest by tasks.registering(JavaExec::class) {
    dependsOn(tasks.testClasses)
    classpath = sourceSets.test.get().runtimeClasspath
    mainClass.set("com.minecraftgod.bridge.EventOutboxTest")
}
tasks.check { dependsOn(outboxTest) }
val eventContractTest by tasks.registering(JavaExec::class) {
    dependsOn(tasks.testClasses)
    classpath = sourceSets.test.get().runtimeClasspath
    mainClass.set("com.minecraftgod.bridge.EventPayloadTest")
    args(layout.buildDirectory.file("contract/events.json").get().asFile.absolutePath)
}
tasks.check { dependsOn(eventContractTest) }
tasks.test { failOnNoDiscoveredTests = false }

// Only used in a disposable verification world, never deployed with the real bridge.
tasks.register<Jar>("voiceProbeJar") {
    dependsOn(tasks.testClasses)
    archiveFileName.set("VoiceProbe.jar")
    destinationDirectory.set(layout.buildDirectory.dir("verification"))
    from(sourceSets.test.get().output) { include("**/VoiceProbePlugin*.class") }
    from("src/test/voice-probe") { include("plugin.yml") }
}

tasks.withType<AbstractArchiveTask>().configureEach {
    isPreserveFileTimestamps = false
    isReproducibleFileOrder = true
}
