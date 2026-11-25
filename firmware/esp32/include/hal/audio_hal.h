// Audio hardware abstraction (Phase 4 readiness). V1 uses the LAPTOP for all audio, so the
// firmware links NullAudio. A future board adds e.g. I2sMicInput (INMP441) and
// I2sSpeakerOutput (MAX98357A) implementing these same interfaces, and streams PCM over the
// existing WebSocket (binary frames) — the backend pipeline already accepts 16 kHz mono PCM WAV.
#pragma once
#include <Arduino.h>

struct AudioFormat {
  uint32_t sampleRate = 16000;
  uint8_t bitsPerSample = 16;
  uint8_t channels = 1;
};

class AudioInput {
 public:
  virtual ~AudioInput() = default;
  virtual bool begin(const AudioFormat& fmt) = 0;
  // Reads up to maxBytes of PCM; returns bytes read (0 if nothing available).
  virtual size_t read(uint8_t* buf, size_t maxBytes) = 0;
  virtual bool available() const = 0;
};

class AudioOutput {
 public:
  virtual ~AudioOutput() = default;
  virtual bool begin(const AudioFormat& fmt) = 0;
  virtual size_t write(const uint8_t* buf, size_t bytes) = 0;
  virtual void setVolume(uint8_t percent) = 0;
  virtual bool available() const = 0;
};

// V1: no audio hardware attached.
class NullAudioInput : public AudioInput {
 public:
  bool begin(const AudioFormat&) override { return false; }
  size_t read(uint8_t*, size_t) override { return 0; }
  bool available() const override { return false; }
};

class NullAudioOutput : public AudioOutput {
 public:
  bool begin(const AudioFormat&) override { return false; }
  size_t write(const uint8_t*, size_t) override { return 0; }
  void setVolume(uint8_t) override {}
  bool available() const override { return false; }
};
