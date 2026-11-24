// ArtCore Test Plug: see CMakeLists.txt. Two classes in one bundle: «ArtCore Test Plug» (no latency) and
// «ArtCore Test Plug Latency» (LATENCY frames late, reported, so a host can compensate).
// Parameters: Gain (-24…+24 dB), Crash (process() aborts), Hang (process() never returns).
// ARTCORE_TESTPLUG_CRASH=load in the environment: initialize() aborts (a plugin that dies while scanned).
#include "public.sdk/source/main/pluginfactory.h"
#include "public.sdk/source/vst/vstsinglecomponenteffect.h"
#include "public.sdk/source/vst/vstparameters.h"
#include "pluginterfaces/base/ibstream.h"
#include "pluginterfaces/vst/ivstparameterchanges.h"
#include "pluginterfaces/vst/ivstaudioprocessor.h"
#include "base/source/fstreamer.h"

#include <chrono>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <thread>
#include <vector>

using namespace Steinberg;
using namespace Steinberg::Vst;

static const int32 LATENCY = 1000;
enum { kGain = 0, kCrash = 1, kHang = 2 };

class TestPlug : public SingleComponentEffect {
public:
    explicit TestPlug(bool latent) : latent(latent) {}
    static FUnknown* plain(void*) { return (IAudioProcessor*)new TestPlug(false); }
    static FUnknown* late(void*) { return (IAudioProcessor*)new TestPlug(true); }

    tresult PLUGIN_API initialize(FUnknown* context) SMTG_OVERRIDE {
        const char* e = std::getenv("ARTCORE_TESTPLUG_CRASH");
        if (e && std::strcmp(e, "load") == 0)
            std::abort();
        tresult r = SingleComponentEffect::initialize(context);
        if (r != kResultOk)
            return r;
        addAudioInput(STR16("In"), SpeakerArr::kStereo);
        addAudioOutput(STR16("Out"), SpeakerArr::kStereo);
        parameters.addParameter(new RangeParameter(STR16("Gain"), kGain, STR16("dB"), -24., 24., 0.));
        parameters.addParameter(STR16("Crash"), nullptr, 1, 0., ParameterInfo::kCanAutomate, kCrash);
        parameters.addParameter(STR16("Hang"), nullptr, 1, 0., ParameterInfo::kCanAutomate, kHang);
        return kResultOk;
    }

    tresult PLUGIN_API setBusArrangements(SpeakerArrangement* ins, int32 nIn, SpeakerArrangement* outs,
                                          int32 nOut) SMTG_OVERRIDE {
        if (nIn != 1 || nOut != 1)
            return kResultFalse;
        int32 c = SpeakerArr::getChannelCount(ins[0]);
        if (c < 1 || c > 8 || c != SpeakerArr::getChannelCount(outs[0]))
            return kResultFalse;
        FCast<AudioBus>(audioInputs.at(0))->setArrangement(ins[0]);
        FCast<AudioBus>(audioOutputs.at(0))->setArrangement(outs[0]);
        return kResultTrue;
    }

    tresult PLUGIN_API canProcessSampleSize(int32 s) SMTG_OVERRIDE {
        return s == kSample32 ? kResultTrue : kResultFalse;
    }
    uint32 PLUGIN_API getLatencySamples() SMTG_OVERRIDE { return latent ? LATENCY : 0; }

    tresult PLUGIN_API setActive(TBool state) SMTG_OVERRIDE {
        if (state) {
            int32 c = SpeakerArr::getChannelCount(FCast<AudioBus>(audioInputs.at(0))->getArrangement());
            delay.assign(c, std::vector<float>(latent ? LATENCY : 0, 0.f));
            pos = 0;
        }
        return SingleComponentEffect::setActive(state);
    }

    tresult PLUGIN_API process(ProcessData& data) SMTG_OVERRIDE {
        if (data.inputParameterChanges) {
            int32 n = data.inputParameterChanges->getParameterCount();
            for (int32 i = 0; i < n; i++) {
                IParamValueQueue* q = data.inputParameterChanges->getParameterData(i);
                ParamValue v;
                int32 off;
                if (q && q->getPointCount() > 0 && q->getPoint(q->getPointCount() - 1, off, v) == kResultTrue)
                    setParamNormalized(q->getParameterId(), v);
            }
        }
        if (getParamNormalized(kCrash) > 0.5)
            std::abort();
        while (getParamNormalized(kHang) > 0.5)
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        if (data.numInputs == 0 || data.numOutputs == 0 || data.numSamples <= 0)
            return kResultOk;
        double db = -24. + 48. * getParamNormalized(kGain);
        float g = (float)std::pow(10., db / 20.);
        AudioBusBuffers& in = data.inputs[0];
        AudioBusBuffers& out = data.outputs[0];
        int32 ch = std::min(in.numChannels, out.numChannels);
        for (int32 c = 0; c < ch; c++) {
            float* src = in.channelBuffers32[c];
            float* dst = out.channelBuffers32[c];
            if (!latent) {
                for (int32 i = 0; i < data.numSamples; i++)
                    dst[i] = src[i] * g;
                continue;
            }
            std::vector<float>& d = delay[c];
            int32 p = pos;
            for (int32 i = 0; i < data.numSamples; i++) {
                float x = src[i];
                dst[i] = d[p] * g;
                d[p] = x;
                p = (p + 1) % LATENCY;
            }
        }
        if (latent)
            pos = (pos + data.numSamples) % LATENCY;
        return kResultOk;
    }

    tresult PLUGIN_API setState(IBStream* s) SMTG_OVERRIDE {
        IBStreamer r(s, kLittleEndian);
        double gain = 0.5;
        if (!r.readDouble(gain))
            return kResultFalse;
        setParamNormalized(kGain, gain);
        return kResultOk;
    }
    tresult PLUGIN_API getState(IBStream* s) SMTG_OVERRIDE {
        IBStreamer w(s, kLittleEndian);
        w.writeDouble(getParamNormalized(kGain));
        return kResultOk;
    }

private:
    bool latent;
    std::vector<std::vector<float>> delay;
    int32 pos = 0;
};

BEGIN_FACTORY_DEF("ArtCore", "https://github.com/Ruf790/artcore-sound", "")
DEF_CLASS2(INLINE_UID(0x41724374, 0x54657374, 0x506C7567, 0x00000001), PClassInfo::kManyInstances,
           kVstAudioEffectClass, "ArtCore Test Plug", 0, "Fx|Tools", "1.0.0", kVstVersionString, TestPlug::plain)
DEF_CLASS2(INLINE_UID(0x41724374, 0x54657374, 0x506C7567, 0x00000002), PClassInfo::kManyInstances,
           kVstAudioEffectClass, "ArtCore Test Plug Latency", 0, "Fx|Tools", "1.0.0", kVstVersionString,
           TestPlug::late)
END_FACTORY
