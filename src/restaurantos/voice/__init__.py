"""Voice (ARCHITECTURE_VOICE_MEDIA §7): the channel-agnostic ``VoiceSession`` (``session``), the ASR bridge and its
``asr_factory`` provider (``asr_bridge``), the 80 ms ASR pacer (``pacer``), the network-free ``FakeASRSession``
(``fake_asr``) and text to speech (``tts``, P15a). The channels sit on top: ``/ws/voice`` (``ptt_ws``, P16b) and the
Twilio phone line (``twilio_http``, ``twilio_media``, ``turns``; P27a, P27b, P28)."""
