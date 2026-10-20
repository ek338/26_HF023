import os

import queue

import warnings

from pathlib import Path



import numpy as np

import sounddevice as sd

import soundfile as sf

import librosa
from scipy.signal import lfilter



from resemblyzer import VoiceEncoder, preprocess_wav



import serial

import time





warnings.filterwarnings("ignore")





# ============================================================

# 기본 설정

# ============================================================



INPUT_SAMPLE_RATE = 48000

MODEL_SAMPLE_RATE = 16000

MIC_DEVICE = 1


# ============================================================
# 음성 전처리 설정
# ============================================================

# 1차 IIR High-pass Filter cutoff
HPF_CUTOFF_HZ = 80.0

# y[n] = alpha * (y[n-1] + x[n] - x[n-1])
_hpf_dt = 1.0 / INPUT_SAMPLE_RATE
_hpf_rc = 1.0 / (2.0 * np.pi * HPF_CUTOFF_HZ)
HPF_ALPHA = _hpf_rc / (_hpf_rc + _hpf_dt)

# MFCC는 보조 특징 추출/분석용으로 계산한다.
# 실제 화자 판정은 기존 Resemblyzer VoiceEncoder embedding을 그대로 사용한다.
MFCC_N = 13
MFCC_N_FFT = 512
MFCC_WIN_LENGTH = 400      # 25 ms @ 16 kHz
MFCC_HOP_LENGTH = 160      # 10 ms @ 16 kHz



ARDUINO_PORT = (

    "/dev/serial/by-id/"

    "usb-Arduino__www.arduino.cc__0042_145333037313516001F0-if00"

)



ARDUINO_BAUD = 9600



arduino = None

last_jammer_state = None



# 화자 등록

REGISTER_COUNT = 5

REGISTER_SEC = 3.0



# 실시간 판별

WINDOW_SEC = 3.0       # 최대 최근 3초 음성으로 화자 판별

FAST_START_SEC = 1.5   # 시작 시 1.5초만 쌓여도 첫 판별 시작

HOP_SEC = 0.5          # 0.5초마다 새 판정



# 연속 판정 횟수

ON_CONFIRM = 2         # 등록자 2회 연속 → JAM ON

OFF_CONFIRM = 2        # 비등록 화자 2회 연속 → JAM OFF



# Threshold

DEFAULT_THRESHOLD = 0.84



# ON threshold보다 이만큼 낮아져야 OFF 후보로 판단

OFF_MARGIN = 0.05



# 무음이 0.5초 × 4회 = 약 2초 연속되면 JAM OFF

SILENCE_CONFIRM = 4



# 너무 짧은 음성 제외

MIN_VALID_SEC = 0.3





# ============================================================

# 경로

# ============================================================



BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = BASE_DIR / "voice_data"



REGISTER_DIR = DATA_DIR / "registered"

TEST_TARGET_DIR = DATA_DIR / "test_target"

TEST_OTHER_DIR = DATA_DIR / "test_other"



PROFILE_PATH = DATA_DIR / "speaker_profile.npy"

THRESHOLD_PATH = DATA_DIR / "threshold.txt"





def make_directories():

    REGISTER_DIR.mkdir(parents=True, exist_ok=True)

    TEST_TARGET_DIR.mkdir(parents=True, exist_ok=True)

    TEST_OTHER_DIR.mkdir(parents=True, exist_ok=True)





# ============================================================

# 공통 함수

# ============================================================



def record_audio(seconds):

    audio = sd.rec(

        int(seconds * INPUT_SAMPLE_RATE),

        samplerate=INPUT_SAMPLE_RATE,

        channels=1,

        dtype="float32",

        device=MIC_DEVICE

    )



    sd.wait()



    return audio.flatten()





def apply_hpf(audio):

    """
    80 Hz 이하의 저주파 성분을 감쇠하는 1차 IIR HPF.

    구현식:
        y[n] = alpha * (y[n-1] + x[n] - x[n-1])
    """

    audio = np.asarray(audio, dtype=np.float32)

    if audio.size == 0:
        return audio

    # 위 식의 전달함수에 해당하는 계수
    b = np.array([HPF_ALPHA, -HPF_ALPHA], dtype=np.float64)
    a = np.array([1.0, -HPF_ALPHA], dtype=np.float64)

    filtered = lfilter(b, a, audio)

    return filtered.astype(np.float32)



def get_rms(audio):

    """음성 에너지의 RMS 값"""

    audio = np.asarray(audio, dtype=np.float32)

    if audio.size == 0:
        return 0.0

    return float(
        np.sqrt(np.mean(np.square(audio)) + 1e-12)
    )



def get_ste(audio):

    """
    STE(Short-Time Energy).

    프레임 길이에 따라 값이 과도하게 달라지지 않도록
    평균 제곱 에너지(mean-square energy)로 계산한다.
    """

    audio = np.asarray(audio, dtype=np.float32)

    if audio.size == 0:
        return 0.0

    return float(
        np.mean(np.square(audio))
    )



def get_peak(audio):

    audio = np.asarray(audio, dtype=np.float32)

    if audio.size == 0:
        return 0.0

    return float(np.max(np.abs(audio)))



def vad_decision(audio, rms_threshold):

    """
    RMS/STE 기반의 단순 VAD.

    기존 코드의 RMS threshold 판정 동작을 유지하면서
    STE를 함께 계산/확인하도록 구성한다.

    STE threshold = RMS threshold^2 이므로
    기존 RMS 판정과 사실상 동일한 기준이다.
    """

    rms = get_rms(audio)
    ste = get_ste(audio)

    ste_threshold = max(
        float(rms_threshold) ** 2,
        1e-12
    )

    is_speech = (
        rms >= rms_threshold
        and
        ste >= ste_threshold
    )

    return is_speech, rms, ste



def preprocess_audio(audio):

    """
    등록/테스트/실시간 음성 모두 동일한 방식으로 전처리.

    1) 80 Hz 1차 IIR HPF
    2) Resemblyzer preprocess_wav
       - 48 kHz 입력을 모델 입력 형식(16 kHz)으로 전처리
       - 기존 Resemblyzer 전처리/정규화 유지
    """

    filtered = apply_hpf(audio)

    wav = preprocess_wav(
        filtered,
        source_sr=INPUT_SAMPLE_RATE
    )

    if len(wav) < int(MIN_VALID_SEC * MODEL_SAMPLE_RATE):
        return None

    return wav



def extract_mfcc(wav):

    """
    16 kHz 전처리 음성에서 13차 MFCC를 추출한다.

    중요:
    MFCC는 보조 특징/분석용으로 계산하며,
    실제 화자 판정 경로는 기존과 동일하게
    Resemblyzer VoiceEncoder embedding을 사용한다.
    """

    wav = np.asarray(wav, dtype=np.float32)

    if wav.size == 0:
        return np.empty((MFCC_N, 0), dtype=np.float32)

    mfcc = librosa.feature.mfcc(
        y=wav,
        sr=MODEL_SAMPLE_RATE,
        n_mfcc=MFCC_N,
        n_fft=MFCC_N_FFT,
        win_length=MFCC_WIN_LENGTH,
        hop_length=MFCC_HOP_LENGTH
    )

    return mfcc.astype(np.float32)



def cosine_similarity(v1, v2):

    return float(

        np.dot(v1, v2)

        /

        (

            np.linalg.norm(v1)

            * np.linalg.norm(v2)

            + 1e-8

        )

    )





# ============================================================

# 1. 화자 등록

# ============================================================



def register_speaker(encoder):



    print("\n" + "=" * 60)

    print(" 화자 등록")

    print("=" * 60)



    print(

        "\n등록자는 자연스럽게 서로 다른 문장을 말해주세요."

    )



    print(

        f"{REGISTER_SEC:.0f}초 녹음을 "

        f"{REGISTER_COUNT}회 진행합니다.\n"

    )



    processed_wavs = []



    for i in range(1, REGISTER_COUNT + 1):



        while True:



            input(

                f"[{i}/{REGISTER_COUNT}] "

                "Enter를 누르면 녹음을 시작합니다..."

            )



            print("🎙️ 녹음 중! 자연스럽게 말하세요.")



            audio = record_audio(REGISTER_SEC)



            rms = get_rms(audio)

            peak = get_peak(audio)



            print(

                f"   RMS={rms:.5f}, Peak={peak:.5f}"

            )



            wav = preprocess_audio(audio)



            if wav is None:

                print(

                    "❌ 사람 음성이 충분히 검출되지 않았습니다."

                )

                print("다시 녹음해주세요.\n")

                continue


            # MFCC는 보조 특징/분석용으로 계산
            mfcc = extract_mfcc(wav)



            filename = REGISTER_DIR / f"reg_mic_{i}.wav"



            sf.write(

                filename,

                audio,

                INPUT_SAMPLE_RATE

            )



            processed_wavs.append(wav)



            print(

                f"✅ 저장 완료: {filename.name}"

            )



            break



    print("\n[모델] 화자 프로필 생성 중...")



    # 여러 등록 음성의 embedding을 평균하고 L2 normalize

    speaker_embedding = encoder.embed_speaker(

        processed_wavs

    )



    np.save(

        PROFILE_PATH,

        speaker_embedding

    )



    print(

        f"✅ 화자 프로필 저장 완료\n"

        f"   {PROFILE_PATH}"

    )



    print(

        f"   embedding 차원: "

        f"{speaker_embedding.shape[0]}"

    )





# ============================================================

# 프로필 로드

# ============================================================



def load_profile():



    if not PROFILE_PATH.exists():



        print(

            "\n❌ 등록된 화자 프로필이 없습니다."

        )



        print(

            "먼저 메뉴 1번으로 화자를 등록해주세요."

        )



        return None



    profile = np.load(PROFILE_PATH)



    # 혹시 모를 normalization

    profile = profile / (

        np.linalg.norm(profile) + 1e-8

    )



    return profile





# ============================================================

# threshold 저장 / 로드

# ============================================================



def save_threshold(threshold):



    with open(

        THRESHOLD_PATH,

        "w",

        encoding="utf-8"

    ) as f:



        f.write(str(threshold))





def load_threshold():



    if not THRESHOLD_PATH.exists():



        print(

            f"⚠️ 보정된 threshold 없음 → "

            f"기본값 {DEFAULT_THRESHOLD} 사용"

        )



        return DEFAULT_THRESHOLD



    with open(

        THRESHOLD_PATH,

        "r",

        encoding="utf-8"

    ) as f:



        return float(f.read().strip())





# ============================================================

# 테스트용 음성 녹음

# ============================================================



def record_test_sample(

    encoder,

    profile,

    label,

    index,

    save_dir

):



    while True:



        input(

            f"[{label} {index}] "

            "Enter를 누르면 3초 녹음..."

        )



        print("🎙️ 녹음 중...")



        audio = record_audio(3)



        wav = preprocess_audio(audio)



        if wav is None:



            print(

                "❌ 음성이 충분하지 않습니다. 다시 시도."

            )



            continue



        # MFCC는 보조 특징/분석용으로 계산
        mfcc = extract_mfcc(wav)

        emb = encoder.embed_utterance(wav)



        similarity = cosine_similarity(

            profile,

            emb

        )



        filename = (

            save_dir /

            f"{label}_{index}.wav"

        )



        sf.write(

            filename,

            audio,

            INPUT_SAMPLE_RATE

        )



        print(

            f"   유사도 = {similarity:.4f}"

        )



        return similarity





# ============================================================

# 최적 threshold 찾기

# ============================================================



def find_best_threshold(target_scores, other_scores):



    all_scores = np.array(

        target_scores + other_scores,

        dtype=float

    )



    unique_scores = sorted(

        np.unique(all_scores)

    )



    candidates = []



    candidates.append(

        unique_scores[0] - 0.0001

    )



    for a, b in zip(

        unique_scores[:-1],

        unique_scores[1:]

    ):



        candidates.append(

            (a + b) / 2

        )



    candidates.append(

        unique_scores[-1] + 0.0001

    )



    best_threshold = None

    best_score = -1

    best_tpr = 0

    best_tnr = 0



    for threshold in candidates:



        # 등록자를 등록자로 맞춘 비율

        tpr = np.mean(

            np.array(target_scores) >= threshold

        )



        # 타인을 타인으로 맞춘 비율

        tnr = np.mean(

            np.array(other_scores) < threshold

        )



        balanced_accuracy = (

            tpr + tnr

        ) / 2



        # 점수 같으면 타인을 더 잘 거부하는 기준 선호

        score = (

            balanced_accuracy,

            tnr,

            tpr

        )



        if score > (

            best_score,

            best_tnr,

            best_tpr

        ):



            best_score = balanced_accuracy

            best_tnr = tnr

            best_tpr = tpr

            best_threshold = threshold



    return (

        best_threshold,

        best_tpr,

        best_tnr

    )





# ============================================================

# 2. Threshold 보정

# ============================================================



def calibrate_threshold(encoder):



    profile = load_profile()



    if profile is None:

        return



    print("\n" + "=" * 60)

    print(" Threshold 보정")

    print("=" * 60)



    print(

        "\n등록할 때 사용하지 않은 새로운 문장으로 테스트합니다."

    )



    print(

        "\n먼저 등록 화자 테스트 5회를 진행합니다."

    )



    target_scores = []



    for i in range(1, 6):



        score = record_test_sample(

            encoder,

            profile,

            "target",

            i,

            TEST_TARGET_DIR

        )



        target_scores.append(score)



    print(

        "\n이제 타인 음성 테스트를 진행합니다."

    )



    print(

        "가능하면 서로 다른 사람 2~3명 이상이 "

        "참여하는 것이 좋습니다."

    )



    other_scores = []



    for i in range(1, 11):



        score = record_test_sample(

            encoder,

            profile,

            "other",

            i,

            TEST_OTHER_DIR

        )



        other_scores.append(score)



    print("\n" + "-" * 60)



    print("등록 화자 점수:")



    for score in target_scores:

        print(f"  {score:.4f}")



    print("\n타인 점수:")



    for score in other_scores:

        print(f"  {score:.4f}")



    target_min = min(target_scores)

    target_mean = np.mean(target_scores)



    other_max = max(other_scores)

    other_mean = np.mean(other_scores)



    print("\n[분포]")



    print(

        f"등록자 평균 : {target_mean:.4f}"

    )



    print(

        f"등록자 최저 : {target_min:.4f}"

    )



    print(

        f"타인 평균   : {other_mean:.4f}"

    )



    print(

        f"타인 최고   : {other_max:.4f}"

    )



    threshold, tpr, tnr = find_best_threshold(

        target_scores,

        other_scores

    )



    save_threshold(threshold)



    print("\n" + "=" * 60)



    print(

        f"추천 Threshold = {threshold:.4f}"

    )



    print(

        f"등록자 검출률 = {tpr * 100:.1f}%"

    )



    print(

        f"타인 거부율   = {tnr * 100:.1f}%"

    )



    print("=" * 60)



    if target_min > other_max:



        print(

            "\n✅ 테스트 데이터에서는 "

            "등록자와 타인이 잘 분리되었습니다."

        )



    else:



        print(

            "\n⚠️ 등록자/타인 유사도 영역이 겹칩니다."

        )



        print(

            "등록 샘플을 늘리거나 환경/마이크를 "

            "다시 확인하는 것을 권장합니다."

        )





# ============================================================

# 재밍 제어

# ============================================================



def connect_arduino():

    global arduino

    global last_jammer_state



    print("\n[Arduino] Mega 연결 중...")



    try:

        arduino = serial.Serial(

            ARDUINO_PORT,

            ARDUINO_BAUD,

            timeout=1

        )



        # Serial을 열면 Mega가 리셋될 수 있음

        time.sleep(2)



        arduino.reset_input_buffer()



        # 시작 상태는 항상 JAM OFF

        arduino.write(b"JAM_OFF\n")

        arduino.flush()



        last_jammer_state = False



        print("✅ Arduino Mega 연결 완료")

        print(

            "⚠️ Mega가 리셋되었으므로 "

            "외부 POWER 버튼을 한 번 눌러 "

            "MASTER:ON 상태로 만들어주세요."

        )



    except serial.SerialException as e:

        arduino = None

        print(f"❌ Arduino 연결 실패: {e}")





def set_jammer(enabled):

    global last_jammer_state



    # 이미 같은 상태라면 같은 명령을 반복하지 않음

    if enabled == last_jammer_state:

        return



    if arduino is None:

        print(

            "❌ Arduino가 연결되어 있지 않아 "

            "JAM 명령을 전송할 수 없습니다."

        )

        return



    try:

        if enabled:



            arduino.write(

                b"JAM_ON\n"

            )



            arduino.flush()



            print(

                "\n🔥🔥🔥 >>> JAMMER ON <<< 🔥🔥🔥\n"

            )



        else:



            arduino.write(

                b"JAM_OFF\n"

            )



            arduino.flush()



            print(

                "\n🟢 >>> JAMMER OFF <<<\n"

            )



        last_jammer_state = enabled



    except serial.SerialException as e:



        print(

            f"❌ Arduino Serial 오류: {e}"

        )



# ============================================================

# 주변 소음 측정

# ============================================================



def measure_noise():



    print("\n[주변 소음 측정]")

    print("2초 동안 말하지 말고 조용히 있어주세요.")



    audio = record_audio(2)



    # VAD 기준도 실시간 블록과 동일하게 HPF 이후 신호에서 계산
    filtered_audio = apply_hpf(audio)

    noise_rms = get_rms(filtered_audio)
    noise_ste = get_ste(filtered_audio)



    # 실제 주변소음 기준으로 동적으로 설정

    # 기존 0.002는 현재 USB 마이크에서 너무 높았음

    voice_threshold = max(

        0.0005,

        noise_rms * 2.0

    )



    print(

        f"주변 소음 RMS = {noise_rms:.6f}"

    )

    print(

        f"주변 소음 STE = {noise_ste:.8f}"

    )



    print(

        f"음성 감지 기준(RMS) = {voice_threshold:.6f}"

    )

    print(

        f"음성 감지 기준(STE) = {voice_threshold ** 2:.8f}"

    )



    return voice_threshold





# ============================================================

# 3. 실시간 화자 판별

# ============================================================



def live_detection(encoder):



    profile = load_profile()



    if profile is None:

        return



    # Calibration에서 저장된 threshold

    on_threshold = load_threshold()



    # ON보다 충분히 낮은 경우에만 OFF 후보

    off_threshold = on_threshold - OFF_MARGIN





    print("\n" + "=" * 60)

    print(" Voice Jammer - 실시간 화자 식별")

    print("=" * 60)



    print(

        f"ON Threshold  : {on_threshold:.4f}"

    )



    print(

        f"OFF Threshold : {off_threshold:.4f}"

    )



    print(

        f"Window        : {WINDOW_SEC:.1f} sec"

    )



    print(

        f"Hop           : {HOP_SEC:.1f} sec"

    )



    print(

        f"JAM ON        : 등록자 {ON_CONFIRM}회 연속"

    )



    print(

        f"JAM OFF       : 비등록 화자 {OFF_CONFIRM}회 연속"

    )



    print(

        f"Silence OFF   : 무음 {SILENCE_CONFIRM}회 연속"

    )

    print(

        f"Preprocess    : HPF {HPF_CUTOFF_HZ:.0f} Hz + RMS/STE VAD + MFCC({MFCC_N})"

    )





    # --------------------------------------------------------

    # 주변 소음 측정

    # --------------------------------------------------------



    voice_rms_threshold = measure_noise()





    # 실제 입력은 48kHz이므로 sample 수도 48kHz 기준

    window_samples = int(

        WINDOW_SEC * INPUT_SAMPLE_RATE

    )



    # 시작 직후에는 1.5초만 모이면 화자 판별을 시작하고,
    # 이후 rolling buffer는 최대 3초까지 자연스럽게 증가함
    fast_start_samples = int(

        FAST_START_SEC * INPUT_SAMPLE_RATE

    )



    hop_samples = int(

        HOP_SEC * INPUT_SAMPLE_RATE

    )





    audio_queue = queue.Queue(

        maxsize=8

    )





    rolling_audio = np.zeros(

        0,

        dtype=np.float32

    )





    # --------------------------------------------------------

    # 판정 상태 변수

    # --------------------------------------------------------



    positive_count = 0

    negative_count = 0

    silence_count = 0



    jammer_on = False





    # --------------------------------------------------------

    # 마이크 callback

    # --------------------------------------------------------



    def audio_callback(

        indata,

        frames,

        time_info,

        status

    ):



        if status:

            print(status)



        block = (

            indata[:, 0]

            .copy()

            .astype(np.float32)

        )



        try:



            audio_queue.put_nowait(

                block

            )



        except queue.Full:



            # Raspberry Pi 처리가 밀리면

            # 가장 오래된 음성을 버리고 최신 음성을 사용

            try:



                audio_queue.get_nowait()



            except queue.Empty:



                pass



            try:



                audio_queue.put_nowait(

                    block

                )



            except queue.Full:



                pass





    print("\n" + "-" * 60)

    print("실시간 분석 시작!")

    print("Ctrl+C로 종료합니다.")

    print("-" * 60 + "\n")





    try:



        with sd.InputStream(

            samplerate=INPUT_SAMPLE_RATE,

            device=MIC_DEVICE,

            channels=1,

            dtype="float32",

            blocksize=hop_samples,

            callback=audio_callback

        ):



            while True:



                block = audio_queue.get()





                # ------------------------------------------------

                # 최근 3초 Sliding Window 구성

                # ------------------------------------------------



                rolling_audio = np.concatenate(

                    (

                        rolling_audio,

                        block

                    )

                )





                if len(rolling_audio) > window_samples:



                    rolling_audio = rolling_audio[

                        -window_samples:

                    ]





                # ------------------------------------------------

                # 현재 0.5초 구간의 음성 에너지 계산

                # ------------------------------------------------



                # VAD는 80 Hz HPF 이후 블록에서 수행
                filtered_block = apply_hpf(block)

                is_speech, block_rms, block_ste = vad_decision(
                    filtered_block,
                    voice_rms_threshold
                )





                # ------------------------------------------------

                # 1) 무음 처리

                # ------------------------------------------------



                if not is_speech:



                    positive_count = 0

                    negative_count = 0



                    silence_count = min(

                        silence_count + 1,

                        SILENCE_CONFIRM

                    )



                    print(

                        f"💤 무음 "

                        f"| RMS={block_rms:.5f} "

                        f"| STE={block_ste:.8f} "

                        f"| {silence_count}/{SILENCE_CONFIRM}"

                    )





                    # JAM이 켜진 상태에서

                    # 약 2초간 무음이 지속되면 OFF

                    if (

                        jammer_on

                        and

                        silence_count >= SILENCE_CONFIRM

                    ):



                        jammer_on = False



                        set_jammer(

                            False

                        )





                    continue





                # 음성이 감지되었으므로 무음 카운터 초기화

                silence_count = 0





                # ------------------------------------------------

                # 빠른 초기 판별

                # ------------------------------------------------

                # 기존에는 3초 전체 window가 찰 때까지 기다렸지만,
                # 이제는 1.5초만 쌓이면 첫 화자 판별을 시작함.
                # 이후에는 2.0 -> 2.5 -> 3.0초로 자동 증가하고,
                # 3초가 찬 뒤에는 계속 최근 3초 sliding window를 사용함.

                if len(rolling_audio) < fast_start_samples:



                    collected_sec = (

                        len(rolling_audio)

                        / INPUT_SAMPLE_RATE

                    )



                    print(

                        f"🎙️ 음성 감지 - 초기 버퍼 수집 중 "

                        f"({collected_sec:.1f}/{FAST_START_SEC:.1f}s)"

                    )



                    continue





                # ------------------------------------------------

                # 전처리

                # 48kHz → 16kHz + VAD/정규화

                # ------------------------------------------------



                processed = preprocess_audio(

                    rolling_audio

                )





                if processed is None:



                    print(

                        "💤 유효한 음성이 충분하지 않음"

                    )



                    continue



                # ------------------------------------------------
                # MFCC 보조 특징 추출
                # ------------------------------------------------
                # 화자 판정 입력 자체는 바꾸지 않고,
                # 분석/검증용 MFCC만 함께 계산한다.
                mfcc = extract_mfcc(processed)





                # ------------------------------------------------

                # 화자 임베딩 생성

                # ------------------------------------------------



                embedding = encoder.embed_utterance(

                    processed

                )





                # ------------------------------------------------

                # 등록 화자와 cosine similarity 계산

                # ------------------------------------------------



                similarity = cosine_similarity(

                    profile,

                    embedding

                )





                # =================================================

                # 2) 등록 화자 후보

                # =================================================



                if similarity >= on_threshold:



                    positive_count = min(

                        positive_count + 1,

                        ON_CONFIRM

                    )



                    negative_count = 0





                    print(

                        f"🔊 등록 화자 후보 "

                        f"| sim={similarity:.4f} "

                        f"| +{positive_count}/{ON_CONFIRM}"

                    )





                # =================================================

                # 3) 비등록 화자 후보

                # =================================================



                elif similarity <= off_threshold:



                    negative_count = min(

                        negative_count + 1,

                        OFF_CONFIRM

                    )



                    positive_count = 0





                    print(

                        f"👤 비등록 화자 후보 "

                        f"| sim={similarity:.4f} "

                        f"| -{negative_count}/{OFF_CONFIRM}"

                    )





                # =================================================

                # 4) Gray Zone

                #

                # ON/OFF 사이에 있는 애매한 점수는

                # 화자 상태를 변경하지 않음

                # =================================================



                else:



                    positive_count = 0

                    negative_count = 0





                    print(

                        f"⚪ 판단 보류 "

                        f"| sim={similarity:.4f} "

                        f"| JAM="

                        f"{'ON' if jammer_on else 'OFF'} 유지"

                    )





                # ------------------------------------------------

                # JAM ON

                # ------------------------------------------------



                if (

                    not jammer_on

                    and

                    positive_count >= ON_CONFIRM

                ):



                    jammer_on = True



                    set_jammer(

                        True

                    )





                # ------------------------------------------------

                # JAM OFF

                # ------------------------------------------------



                elif (

                    jammer_on

                    and

                    negative_count >= OFF_CONFIRM

                ):



                    jammer_on = False



                    set_jammer(

                        False

                    )





    except KeyboardInterrupt:



        if jammer_on:



            set_jammer(

                False

            )



        print(

            "\n실시간 분석을 종료했습니다."

        )





# ============================================================

# 메인 메뉴

# ============================================================



def main():



    make_directories()



    connect_arduino()



    print("=" * 60)

    print(" Voice Jammer Speaker Recognition")

    print("=" * 60)



    print("\n[모델] Resemblyzer 로딩 중...")



    encoder = VoiceEncoder()



    print("✅ 모델 로딩 완료")



    while True:



        print("\n" + "=" * 60)



        print("1. 등록 화자 새로 등록")

        print("2. Threshold 테스트 / 보정")

        print("3. 실시간 화자 판별")

        print("4. 종료")



        print("=" * 60)



        choice = input(

            "\n선택: "

        ).strip()



        if choice == "1":



            register_speaker(

                encoder

            )



        elif choice == "2":



            calibrate_threshold(

                encoder

            )



        elif choice == "3":



            live_detection(

                encoder

            )



        elif choice == "4":



            print(

                "\n종료합니다."

            )



            break



        else:



            print(

                "❌ 1~4 중 하나를 입력해주세요."

            )





if __name__ == "__main__":

    main()
