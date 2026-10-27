/**
 * Audio libraries for the voice surfaces (CONTRACTS_API §5, ARCHITECTURE_VOICE_MEDIA §7.1 and §7.5): microphone
 * capture through the PCM worklet, push-to-talk turns, the `/ws/voice` client, gapless TTS playback, the
 * browser-speech fallback and PCM/WAV helpers. Used by the kiosk, the recorder, the action page and the kitchen slot.
 */

export {
  browserAudioDeps,
  captureProblem,
  MIC_CONSTRAINTS,
  MicCapture,
  WORKLET_NAME,
  WORKLET_URL,
  type AudioBufferLike,
  type AudioContextLike,
  type AudioDeps,
  type AudioNodeLike,
  type BufferSourceLike,
  type CaptureState,
  type WorkletNodeLike,
  type WorkletNodeOptions,
} from './capture'
export {
  concatPcm,
  encodeWav,
  floatToPcm16,
  FRAME_BYTES,
  FRAME_MS,
  FRAME_SAMPLES,
  MAX_TURN_MS,
  Pcm16Reader,
  pcm16FromBytes,
  pcm16ToFloat,
  rmsLevel,
  SAMPLE_RATE,
  samplesToMs,
} from './pcm'
export { PcmPlayer, type PcmPlayerOptions } from './player'
export { MIN_HOLD_MS, PushToTalk, type PttEnd, type PttPhase, type PushToTalkOptions } from './ptt'
export {
  browserSpeech,
  loadVoices,
  pickVoice,
  speakText,
  type SpeakHandle,
  type SpeechDeps,
  type UtteranceLike,
  type VoiceLike,
} from './speech'
export { isEditable, useHoldToTalk, type HoldToTalkHandlers, type HoldToTalkOptions } from './useHoldToTalk'
export {
  isVoiceSessionId,
  newTurnKey,
  parseVoiceMessage,
  PROBE_AFTER,
  VOICE_BACKOFF_MS,
  VoiceSocket,
  voiceUrl,
  type VoiceLang,
  type VoicePurpose,
  type VoiceQuery,
  type VoiceSocketLike,
  type VoiceSocketOptions,
  type VoiceStatus,
} from './voiceSocket'
