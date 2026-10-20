#include <Arduino.h>
#include <avr/interrupt.h>

// ======================================================
// MA40S4S x30 / JMOD x5
//
// Mega D2 -> JMOD1~5 AIN1 + BIN1
// Mega D3 -> JMOD1~5 AIN2 + BIN2
// Mega D4 -> JMOD1~5 PWMA + PWMB
// Mega D8 -> JMOD1~5 STBY
//
// 각 JMOD:
//   AOUT1-AOUT2 -> 트랜스듀서 3개 병렬
//   BOUT1-BOUT2 -> 트랜스듀서 3개 병렬
//
// JMOD 1개당 6개
// 총 5개 JMOD = 30개
//
// Carrier       : 40 kHz
// Modulation     : Pseudo-random AM/PDM
// Mod update     : 8 kHz
// Average gating : 약 75%
//
// Control        : Raspberry Pi USB Serial (9600 bps)
//                  "JAM_ON\n"  -> OUTPUT ON
//                  "JAM_OFF\n" -> OUTPUT OFF
//                  (시리얼 모니터 수동 명령 1 / 0 / S 도 사용 가능)
// ======================================================


#define IN1   2
#define IN2   3
#define PWM   4
#define STBY  8


// ======================================================
// 상태 변수
// ======================================================

volatile bool outputEnabled = false;

volatile bool phase40k = false;


// 16-bit Galois LFSR
volatile uint16_t lfsr = 0xACE1u;


// 8-bit PDM accumulator
volatile uint8_t noiseAccumulator = 0;


// ======================================================
// Raspberry Pi 명령 수신 버퍼
//
// speaker_identification.py가 줄 단위로 전송
//
//   "JAM_ON\n"
//   "JAM_OFF\n"
// ======================================================

#define CMD_BUFFER_SIZE  16

char cmdBuffer[CMD_BUFFER_SIZE];

uint8_t cmdLength = 0;


// ======================================================
// TIMER1 ISR
//
// Interrupt frequency = 80 kHz
//
// 매 interrupt마다 phase 반전
//
// 80 kHz / 2
// = 40 kHz carrier
//
// Arduino Mega 2560
//
// D2 = PE4
// D3 = PE5
// ======================================================

ISR(TIMER1_COMPA_vect)
{
  if (!outputEnabled)
  {
    // D2 LOW
    // D3 LOW

    PORTE &= ~(_BV(PE4) | _BV(PE5));

    return;
  }


  phase40k = !phase40k;


  if (phase40k)
  {
    // --------------------------------
    // D2 HIGH
    // D3 LOW
    // --------------------------------

    PORTE |= _BV(PE4);

    PORTE &= ~_BV(PE5);
  }

  else
  {
    // --------------------------------
    // D2 LOW
    // D3 HIGH
    // --------------------------------

    PORTE &= ~_BV(PE4);

    PORTE |= _BV(PE5);
  }
}


// ======================================================
// TIMER3 ISR
//
// 8 kHz마다 pseudo-random 값 생성
//
// D4(PWM)를 PDM 방식으로 gating
//
//      40 kHz Carrier
//             ×
//      Random AM / PDM
//
// 평균 carrier ON ≈ 75%
//
// Mega D4 = PG5
// ======================================================

ISR(TIMER3_COMPA_vect)
{
  if (!outputEnabled)
  {
    // PWM LOW

    PORTG &= ~_BV(PG5);

    return;
  }


  // ====================================================
  // 16-bit Galois LFSR
  // ====================================================

  uint16_t feedback = lfsr & 1u;


  lfsr >>= 1;


  if (feedback)
  {
    lfsr ^= 0xB400u;
  }


  // ====================================================
  // Pseudo-random 값
  //
  // r = 0 ~ 255
  // ====================================================

  uint8_t r =
      (uint8_t)(lfsr & 0xFF);


  // ====================================================
  // ★ 약 75% 평균 gating
  //
  // 기존:
  //
  // noiseValue = 0 ~ 255
  // 평균 ≈ 127.5
  // 평균 duty ≈ 50%
  //
  //
  // 현재:
  //
  // r >> 1
  // = 0 ~ 127
  //
  // +128
  //
  // noiseValue = 128 ~ 255
  //
  // 평균 ≈ 191.5
  //
  // 191.5 / 256
  // ≈ 74.8%
  // ====================================================

  uint8_t noiseValue =
      128 + (r >> 1);


  // ====================================================
  // 8-bit Pulse Density Modulation
  // ====================================================

  uint16_t sum =
      (uint16_t)noiseAccumulator
      +
      (uint16_t)noiseValue;


  if (sum >= 256)
  {
    // --------------------------------
    // Carrier 통과
    // PWM HIGH
    // --------------------------------

    PORTG |= _BV(PG5);
  }

  else
  {
    // --------------------------------
    // Carrier 차단
    // PWM LOW
    // --------------------------------

    PORTG &= ~_BV(PG5);
  }


  // 하위 8 bit 유지

  noiseAccumulator =
      (uint8_t)sum;
}


// ======================================================
// TIMER1 설정
//
// Carrier = 40 kHz
// ======================================================

void setupCarrier40kHz()
{
  cli();


  TCCR1A = 0;

  TCCR1B = 0;


  TCNT1 = 0;


  // ====================================================
  // Arduino Mega = 16 MHz
  //
  // 16 MHz / (199 + 1)
  //
  // = 80 kHz interrupt
  //
  // 두 번의 interrupt가 carrier 한 주기
  //
  // 80 kHz / 2
  //
  // = 40 kHz
  // ====================================================

  OCR1A = 199;


  // CTC Mode

  TCCR1B |=
      (1 << WGM12);


  // Prescaler = 1

  TCCR1B |=
      (1 << CS10);


  // Compare Match A Interrupt Enable

  TIMSK1 |=
      (1 << OCIE1A);


  sei();
}


// ======================================================
// TIMER3 설정
//
// Pseudo-random AM/PDM update
//
// = 8 kHz
// ======================================================

void setupNoiseAM()
{
  cli();


  TCCR3A = 0;

  TCCR3B = 0;


  TCNT3 = 0;


  // ====================================================
  // Arduino Mega = 16 MHz
  //
  // Prescaler = 8
  //
  // 16 MHz / 8 / (249 + 1)
  //
  // = 8000 Hz
  //
  // = 8 kHz
  // ====================================================

  OCR3A = 249;


  // CTC mode

  TCCR3B |=
      (1 << WGM32);


  // Prescaler = 8

  TCCR3B |=
      (1 << CS31);


  // Compare Match A Interrupt Enable

  TIMSK3 |=
      (1 << OCIE3A);


  sei();
}


// ======================================================
// OUTPUT ON
// ======================================================

void startOutput()
{
  if (outputEnabled)
  {
    return;
  }


  // --------------------------------
  // 초기상태 reset
  // --------------------------------

  phase40k = false;


  noiseAccumulator = 0;


  // LFSR seed 초기화

  lfsr = 0xACE1u;


  // --------------------------------
  // JMOD Enable
  // --------------------------------

  digitalWrite(
      STBY,
      HIGH
  );


  // PWM은 Timer3 ISR에서 제어

  digitalWrite(
      PWM,
      LOW
  );


  // --------------------------------
  // 출력 시작
  // --------------------------------

  outputEnabled = true;


  Serial.println();

  Serial.println(
      "======================================"
  );

  Serial.println(
      "OUTPUT ON"
  );

  Serial.println(
      "MA40S4S x30 / JMOD x5"
  );

  Serial.println(
      "40 kHz + PSEUDO-RANDOM AM/PDM"
  );

  Serial.println(
      "AM/PDM update = 8 kHz"
  );

  Serial.println(
      "Average carrier gating ~= 75%"
  );

  Serial.println(
      "======================================"
  );
}


// ======================================================
// OUTPUT OFF
// ======================================================

void stopOutput()
{
  outputEnabled = false;


  // ISR이 출력 LOW로 정리할 시간

  delayMicroseconds(50);


  // PWM OFF

  digitalWrite(
      PWM,
      LOW
  );


  // JMOD Disable

  digitalWrite(
      STBY,
      LOW
  );


  // 입력 LOW

  digitalWrite(
      IN1,
      LOW
  );


  digitalWrite(
      IN2,
      LOW
  );


  Serial.println();

  Serial.println(
      "======================================"
  );

  Serial.println(
      "OUTPUT OFF"
  );

  Serial.println(
      "======================================"
  );
}


// ======================================================
// STATUS
// ======================================================

void printStatus()
{
  Serial.print(
      "OUTPUT = "
  );


  if (outputEnabled)
  {
    Serial.println(
        "ON"
    );
  }

  else
  {
    Serial.println(
        "OFF"
    );
  }


  Serial.println(
      "Carrier = 40 kHz"
  );

  Serial.println(
      "AM/PDM update = 8 kHz"
  );

  Serial.println(
      "Average gating ~= 75%"
  );
}


// ======================================================
// Raspberry Pi 명령 처리
//
// 줄 끝('\n' 또는 '\r')을 받으면 버퍼 내용을 비교
// ======================================================

void handleCommandLine()
{
  cmdBuffer[cmdLength] = '\0';


  if (strcmp(cmdBuffer, "JAM_ON") == 0)
  {
    startOutput();
  }

  else if (strcmp(cmdBuffer, "JAM_OFF") == 0)
  {
    stopOutput();
  }


  cmdLength = 0;
}


// ======================================================
// SETUP
// ======================================================

void setup()
{
  Serial.begin(9600);


  // --------------------------------
  // Pin Mode
  // --------------------------------

  pinMode(
      IN1,
      OUTPUT
  );


  pinMode(
      IN2,
      OUTPUT
  );


  pinMode(
      PWM,
      OUTPUT
  );


  pinMode(
      STBY,
      OUTPUT
  );


  // --------------------------------
  // 부팅 시 반드시 OFF
  // --------------------------------

  digitalWrite(
      IN1,
      LOW
  );


  digitalWrite(
      IN2,
      LOW
  );


  digitalWrite(
      PWM,
      LOW
  );


  digitalWrite(
      STBY,
      LOW
  );


  // --------------------------------
  // Timer 설정
  // --------------------------------

  setupCarrier40kHz();


  setupNoiseAM();


  // --------------------------------
  // Serial Menu
  // --------------------------------

  Serial.println();

  Serial.println(
      "======================================"
  );

  Serial.println(
      " MA40S4S x30 / JMOD x5"
  );

  Serial.println(
      " 40 kHz PSEUDO-RANDOM AM/PDM"
  );

  Serial.println(
      " Average gating ~= 75%"
  );

  Serial.println(
      "======================================"
  );

  Serial.println(
      "1 : OUTPUT ON"
  );

  Serial.println(
      "0 : OUTPUT OFF"
  );

  Serial.println(
      "S : STATUS"
  );

  Serial.println(
      "JAM_ON / JAM_OFF : Raspberry Pi"
  );

  Serial.println(
      "======================================"
  );
}


// ======================================================
// LOOP
// ======================================================

void loop()
{
  while (Serial.available())
  {
    char cmd =
        Serial.read();


    // ==================================================
    // 줄 끝 -> Raspberry Pi 명령 처리
    // ==================================================

    if (cmd == '\n' || cmd == '\r')
    {
      if (cmdLength > 0)
      {
        handleCommandLine();
      }

      continue;
    }


    // ==================================================
    // 시리얼 모니터 수동 명령 (1 / 0 / S)
    //
    // 줄의 첫 글자일 때만 처리
    // ("JAM_ON" 같은 문자열 중간 글자와 구분)
    // ==================================================

    if (cmdLength == 0)
    {
      // ON

      if (cmd == '1')
      {
        startOutput();

        continue;
      }


      // OFF

      else if (cmd == '0')
      {
        stopOutput();

        continue;
      }


      // STATUS

      else if (
        cmd == 'S'
        ||
        cmd == 's'
      )
      {
        printStatus();

        continue;
      }
    }


    // ==================================================
    // Raspberry Pi 명령 문자 누적
    // ==================================================

    if (cmdLength < CMD_BUFFER_SIZE - 1)
    {
      cmdBuffer[cmdLength++] = cmd;
    }

    else
    {
      // 너무 긴 입력은 버림

      cmdLength = 0;
    }
  }
}
