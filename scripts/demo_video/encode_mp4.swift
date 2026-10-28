// Encode numbered JPEG frames plus a WAV narration into an H.264/AAC MP4 with AVFoundation (no ffmpeg needed).
// Build:  swiftc -O scripts/demo_video/encode_mp4.swift -o data/video_work/encode_mp4
// Run:    encode_mp4 <frames dir with 00000.jpg...> <fps> <voice.wav> <out.mp4> <width> <height>
// Video and audio are fed side by side (requestMediaDataWhenReady on two queues): AVAssetWriter interleaves its
// inputs, so writing all the video first would stall once its buffer filled.

import AVFoundation
import CoreGraphics
import Foundation
import ImageIO

func fail(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(1)
}

let args = CommandLine.arguments
guard args.count == 7, let fps = Int32(args[2]), let width = Int(args[5]), let height = Int(args[6]) else {
    fail("usage: encode_mp4 <frames dir> <fps> <voice.wav> <out.mp4> <width> <height>")
}
let framesDir = URL(fileURLWithPath: args[1])
let wavURL = URL(fileURLWithPath: args[3])
let outURL = URL(fileURLWithPath: args[4])
try? FileManager.default.removeItem(at: outURL)

let frames = (try? FileManager.default.contentsOfDirectory(atPath: framesDir.path))?
    .filter { $0.hasSuffix(".jpg") }.sorted() ?? []
if frames.isEmpty { fail("no frames in \(framesDir.path)") }

guard let writer = try? AVAssetWriter(outputURL: outURL, fileType: .mp4) else { fail("cannot write \(outURL.path)") }
writer.shouldOptimizeForNetworkUse = true  // moov first, so a browser can start playing at once
let videoInput = AVAssetWriterInput(mediaType: .video, outputSettings: [
    AVVideoCodecKey: AVVideoCodecType.h264,
    AVVideoWidthKey: width,
    AVVideoHeightKey: height,
    AVVideoCompressionPropertiesKey: [
        AVVideoAverageBitRateKey: 1_400_000,  // slides and screenshots: mostly still frames
        AVVideoProfileLevelKey: AVVideoProfileLevelH264HighAutoLevel,
        AVVideoMaxKeyFrameIntervalKey: Int(fps) * 4,
    ],
])
videoInput.expectsMediaDataInRealTime = false
let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: videoInput, sourcePixelBufferAttributes: [
    kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
    kCVPixelBufferWidthKey as String: width,
    kCVPixelBufferHeightKey as String: height,
])
writer.add(videoInput)

let audioAsset = AVURLAsset(url: wavURL)
let audioTrack = audioAsset.tracks(withMediaType: .audio).first
let audioInput = AVAssetWriterInput(mediaType: .audio, outputSettings: [
    AVFormatIDKey: kAudioFormatMPEG4AAC,
    AVSampleRateKey: 44_100,
    AVNumberOfChannelsKey: 2,
    AVEncoderBitRateKey: 96_000,
])
audioInput.expectsMediaDataInRealTime = false
if audioTrack != nil { writer.add(audioInput) }

var reader: AVAssetReader?
var audioOutput: AVAssetReaderTrackOutput?
if let track = audioTrack {
    guard let r = try? AVAssetReader(asset: audioAsset) else { fail("cannot read \(wavURL.path)") }
    let out = AVAssetReaderTrackOutput(track: track, outputSettings: [AVFormatIDKey: kAudioFormatLinearPCM])
    r.add(out)
    reader = r
    audioOutput = out
}

guard writer.startWriting() else { fail("writer failed to start: \(String(describing: writer.error))") }
writer.startSession(atSourceTime: .zero)
reader?.startReading()

let group = DispatchGroup()
let colorSpace = CGColorSpaceCreateDeviceRGB()
var next = 0
group.enter()
videoInput.requestMediaDataWhenReady(on: DispatchQueue(label: "video")) {
    while videoInput.isReadyForMoreMediaData {
        if next >= frames.count {
            videoInput.markAsFinished()
            group.leave()
            return
        }
        autoreleasepool {
            let url = framesDir.appendingPathComponent(frames[next])
            guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
                  let image = CGImageSourceCreateImageAtIndex(source, 0, nil),
                  let pool = adaptor.pixelBufferPool else { fail("cannot read frame \(frames[next])") }
            var buffer: CVPixelBuffer?
            CVPixelBufferPoolCreatePixelBuffer(nil, pool, &buffer)
            guard let pixels = buffer else { fail("no pixel buffer") }
            CVPixelBufferLockBaseAddress(pixels, [])
            let context = CGContext(data: CVPixelBufferGetBaseAddress(pixels), width: width, height: height,
                                    bitsPerComponent: 8, bytesPerRow: CVPixelBufferGetBytesPerRow(pixels),
                                    space: colorSpace,
                                    bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue
                                        | CGBitmapInfo.byteOrder32Little.rawValue)
            context?.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
            CVPixelBufferUnlockBaseAddress(pixels, [])
            if !adaptor.append(pixels, withPresentationTime: CMTime(value: CMTimeValue(next), timescale: fps)) {
                fail("frame \(frames[next]) not appended: \(String(describing: writer.error))")
            }
        }
        next += 1
    }
}
if let out = audioOutput {
    group.enter()
    audioInput.requestMediaDataWhenReady(on: DispatchQueue(label: "audio")) {
        while audioInput.isReadyForMoreMediaData {
            guard let sample = out.copyNextSampleBuffer() else {
                audioInput.markAsFinished()
                group.leave()
                return
            }
            if !audioInput.append(sample) { fail("audio not appended: \(String(describing: writer.error))") }
        }
    }
}
group.wait()

let done = DispatchSemaphore(value: 0)
writer.finishWriting { done.signal() }
done.wait()
if writer.status != .completed { fail("encode failed: \(String(describing: writer.error))") }
// The network-optimised rewrite can leave its first-pass "<out>.sb-…" draft beside the video: remove it.
let folder = outURL.deletingLastPathComponent()
for name in (try? FileManager.default.contentsOfDirectory(atPath: folder.path)) ?? []
    where name.hasPrefix(outURL.lastPathComponent + ".sb-") {
    try? FileManager.default.removeItem(at: folder.appendingPathComponent(name))
}
print("encoded \(frames.count) frames to \(outURL.path)")
