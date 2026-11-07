// Smart-PhySystem rover: motor test sketch + streaming speed control
// Arduino Uno + L298N (tank drive) + HC-SR04. Same pins as the main firmware.
//
// Open Serial Monitor at 115200 baud (any line ending) and type a letter:
//   f = forward   b = backward   l = turn left   r = turn right
//   s = stop      d = show distance   ? = help
//
// Streaming mode (used by face_follow.py):
//   V <left> <right>\n     each value -255..255 (negative = backwards)
//   e.g. "V 120 -120" spins right, "V 0 0" holds still.
//   The motors keep that speed until the next V line. If no V line arrives
//   for STREAM_TIMEOUT_MS the motors stop (safety watchdog if the PC script
//   crashes or the cable is pulled). Any letter command leaves streaming mode.
//   While streaming, a status line is printed every STATUS_MS:
//     "S <cm> ok"  or  "S <cm> blocked"   (cm = -1 means no echo)
//
// Distance gate, same rule for letter commands and streaming:
//   - no echo (sensor missing / not working)  -> motors run
//   - reading below FAR_CM (close / tiny)     -> motors run
//   - reading at or above FAR_CM (far away)   -> motors do NOT run

#include <Arduino.h>

// ---- Settings you may want to change ----
const int  DRIVE_SPEED  = 160;      // PWM 0..255 for f / b
const int  TURN_SPEED   = 160;      // PWM 0..255 for l / r
const unsigned long MOVE_MS = 500;  // how long each letter command runs
const long FAR_CM       = 10;      // readings >= this count as "far" and block motion
const bool INVERT_LEFT  = false;    // flip if the left wheel spins backwards on f
const bool INVERT_RIGHT = false;    // flip if the right wheel spins backwards on f
const unsigned long SONAR_TIMEOUT_US = 25000;  // ~4 m; no echo after this = "missing"

const unsigned long STREAM_TIMEOUT_MS = 400;   // stop if no V command for this long
const unsigned long STREAM_SONAR_MS   = 60;    // distance re-check period while streaming
const unsigned long STATUS_MS         = 200;   // status line period while streaming

// ---- Pins (left = L298N channel A, right = channel B) ----
const uint8_t PIN_ENA  = 9;   // left PWM
const uint8_t PIN_IN1  = 8;   // left dir
const uint8_t PIN_IN2  = 7;
const uint8_t PIN_ENB  = 3;   // right PWM
const uint8_t PIN_IN3  = 5;   // right dir
const uint8_t PIN_IN4  = 4;
const uint8_t PIN_TRIG = 11;
const uint8_t PIN_ECHO = 12;

// ---- Streaming state ----
bool streaming = false;
int  streamL = 0, streamR = 0;
unsigned long lastCmdMs = 0, lastSonarMs = 0, lastStatusMs = 0;
long lastCm = -1;

char lineBuf[24];
uint8_t lineLen = 0;
bool inLine = false;

// ---------------------------------------------------------------------------
void setMotor(uint8_t pinEn, uint8_t pinA, uint8_t pinB, int speed, bool invert) {
  if (invert) speed = -speed;
  if (speed > 0) {
    digitalWrite(pinA, HIGH);
    digitalWrite(pinB, LOW);
    analogWrite(pinEn, min(speed, 255));
  } else if (speed < 0) {
    digitalWrite(pinA, LOW);
    digitalWrite(pinB, HIGH);
    analogWrite(pinEn, min(-speed, 255));
  } else {
    digitalWrite(pinA, LOW);   // both LOW with enable HIGH = brake
    digitalWrite(pinB, LOW);
    analogWrite(pinEn, 255);
  }
}

void setMotors(int left, int right) {
  setMotor(PIN_ENA, PIN_IN1, PIN_IN2, left, INVERT_LEFT);
  setMotor(PIN_ENB, PIN_IN3, PIN_IN4, right, INVERT_RIGHT);
}

void stopMotors() {
  setMotors(0, 0);
}

// Returns distance in cm, or -1 if there was no echo.
long readDistanceCm() {
  digitalWrite(PIN_TRIG, LOW);
  delayMicroseconds(2);
  digitalWrite(PIN_TRIG, HIGH);
  delayMicroseconds(10);
  digitalWrite(PIN_TRIG, LOW);
  unsigned long us = pulseIn(PIN_ECHO, HIGH, SONAR_TIMEOUT_US);
  if (us == 0) return -1;
  return (long)(us / 58UL);
}

// Missing reading or a reading closer than FAR_CM -> allowed.
bool motionAllowed(long cm) {
  return cm < 0 || cm < FAR_CM;
}

void printDistance(long cm) {
  if (cm < 0) Serial.print("no echo");
  else { Serial.print(cm); Serial.print(" cm"); }
}

// Runs one move for MOVE_MS, re-checking the sensor as it goes.
void drive(int left, int right, const char *name) {
  long cm = readDistanceCm();
  Serial.print(name);
  Serial.print("  (distance: ");
  printDistance(cm);
  Serial.print(") -> ");

  if (!motionAllowed(cm)) {
    stopMotors();
    Serial.println("BLOCKED, reading is far");
    return;
  }

  setMotors(left, right);
  unsigned long start = millis();
  while (millis() - start < MOVE_MS) {
    // 's' typed during a move stops it right away
    if (Serial.available() > 0) {
      char c = Serial.peek();
      if (c == 's' || c == 'S') {
        Serial.read();
        stopMotors();
        Serial.println("stopped by s");
        return;
      }
    }
    delay(50);
    cm = readDistanceCm();
    if (!motionAllowed(cm)) {
      stopMotors();
      Serial.print("BLOCKED mid-move, reading went far: ");
      printDistance(cm);
      Serial.println();
      return;
    }
  }
  stopMotors();
  Serial.println("done");
}

void printHelp() {
  Serial.println("Commands: f=forward b=backward l=left r=right s=stop d=distance ?=help");
  Serial.println("Streaming: V <left> <right>  (each -255..255, watchdog stops motors)");
  Serial.print("Motors run only if distance is missing or under ");
  Serial.print(FAR_CM);
  Serial.println(" cm.");
}

// ---- Streaming ------------------------------------------------------------
void applyStreamOutputs() {
  if (motionAllowed(lastCm)) setMotors(streamL, streamR);
  else stopMotors();
}

// buf holds whatever followed the 'V', e.g. " 120 -80"
void handleVelocity(char *buf) {
  char *p = buf;
  long l = strtol(p, &p, 10);
  long r = strtol(p, &p, 10);
  streamL = (int)constrain(l, -255, 255);
  streamR = (int)constrain(r, -255, 255);

  unsigned long now = millis();
  if (!streaming) {             // entering stream mode: get a fresh reading first
    streaming = true;
    lastCm = readDistanceCm();
    lastSonarMs = now;
    lastStatusMs = 0;
  }
  lastCmdMs = now;
  applyStreamOutputs();
}

void serviceStream() {
  if (!streaming) return;
  unsigned long now = millis();

  if (now - lastCmdMs > STREAM_TIMEOUT_MS) {
    streaming = false;
    stopMotors();
    Serial.println("S timeout");
    return;
  }
  if (now - lastSonarMs >= STREAM_SONAR_MS) {
    lastSonarMs = now;
    lastCm = readDistanceCm();
    applyStreamOutputs();
  }
  if (now - lastStatusMs >= STATUS_MS) {
    lastStatusMs = now;
    Serial.print("S ");
    Serial.print(lastCm);
    Serial.println(motionAllowed(lastCm) ? " ok" : " blocked");
  }
}

// ---- Letter commands ------------------------------------------------------
void handleLetter(char c) {
  switch (c) {
    case '\n': case '\r': case ' ': case '\t': return;  // ignore line endings/spaces
  }
  if (streaming) { streaming = false; stopMotors(); }   // any letter leaves streaming

  switch (c) {
    case 'f': case 'F': drive( DRIVE_SPEED,  DRIVE_SPEED, "forward");  break;
    case 'b': case 'B': drive(-DRIVE_SPEED, -DRIVE_SPEED, "backward"); break;
    case 'l': case 'L': drive(-TURN_SPEED,   TURN_SPEED,  "left");     break;
    case 'r': case 'R': drive( TURN_SPEED,  -TURN_SPEED,  "right");    break;
    case 's': case 'S': stopMotors(); Serial.println("stop"); break;
    case 'd': case 'D': {
      long cm = readDistanceCm();
      Serial.print("distance: ");
      printDistance(cm);
      Serial.println(motionAllowed(cm) ? "  (motors allowed)" : "  (motors blocked)");
      break;
    }
    case '?': printHelp(); break;
    default:
      Serial.print("unknown command: ");
      Serial.println(c);
      break;
  }
}

// ---------------------------------------------------------------------------
void setup() {
  pinMode(PIN_ENA, OUTPUT);
  pinMode(PIN_ENB, OUTPUT);
  pinMode(PIN_IN1, OUTPUT);
  pinMode(PIN_IN2, OUTPUT);
  pinMode(PIN_IN3, OUTPUT);
  pinMode(PIN_IN4, OUTPUT);
  pinMode(PIN_TRIG, OUTPUT);
  pinMode(PIN_ECHO, INPUT);
  digitalWrite(PIN_TRIG, LOW);
  stopMotors();

  Serial.begin(115200);
  Serial.println("Motor test ready.");
  printHelp();
}

void loop() {
  while (Serial.available() > 0) {
    char c = Serial.read();

    if (inLine) {                               // collecting a "V ..." line
      if (c == '\n' || c == '\r') {
        lineBuf[lineLen] = '\0';
        inLine = false;
        handleVelocity(lineBuf);
      } else if (lineLen < sizeof(lineBuf) - 1) {
        lineBuf[lineLen++] = c;
      } else {                                  // garbage / too long: drop it
        inLine = false;
      }
      continue;
    }

    if (c == 'v' || c == 'V') { inLine = true; lineLen = 0; continue; }
    handleLetter(c);
  }

  serviceStream();
}
