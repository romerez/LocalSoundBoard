# Minification is disabled (personal sideload build) — rules kept for the day
# it's turned on. kotlinx.serialization needs its serializers preserved:
-keepattributes *Annotation*, InnerClasses
-dontnote kotlinx.serialization.**
-keep,includedescriptorclasses class com.romerez.lsbmobile.**$$serializer { *; }
-keepclassmembers class com.romerez.lsbmobile.** {
    *** Companion;
}
-keepclasseswithmembers class com.romerez.lsbmobile.** {
    kotlinx.serialization.KSerializer serializer(...);
}
