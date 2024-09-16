void setup() {
  DDRB |= (1 << PB3);
  DDRB &= ~(1 << PB4);
  PORTB |= (1 << PB3);
  delay(1000);
}

void loop() {
  cli();
  for(;;) {
    if (PINB & (1 << PB4)) {
      PORTB |= (1 << PB3);
    } else {
      PORTB &= ~(1 << PB3);
    }
  }
}
