package example;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

public class CalculatorTest {

    private Calculator calculator;

    @BeforeEach
    void setUp() {
        calculator = new Calculator();
    }

    @Test
    @DisplayName("Test add operation")
    void testAdd() {
        assertEquals(5, calculator.add(2, 3));
        assertEquals(-1, calculator.add(2, -3));
        assertEquals(0, calculator.add(0, 0));
    }

    @Test
    @DisplayName("Test subtract operation")
    void testSub() {
        assertEquals(1, calculator.sub(3, 2));
        assertEquals(5, calculator.sub(2, -3));
        assertEquals(0, calculator.sub(0, 0));
    }

    @Test
    @DisplayName("Test multiply operation")
    void testMul() {
        assertEquals(6, calculator.mul(2, 3));
        assertEquals(-6, calculator.mul(2, -3));
        assertEquals(0, calculator.mul(0, 5));
    }

    @Test
    @DisplayName("Test divide operation")
    void testDiv() {
        assertEquals(2, calculator.div(6, 3));
        assertEquals(-2, calculator.div(6, -3));
        assertEquals(0, calculator.div(0, 5));
    }

    @Test
    @DisplayName("Test divide by zero")
    void testDivByZero() {
        IllegalArgumentException exception = assertThrows(IllegalArgumentException.class, () -> {
            calculator.div(10, 0);
        });
        assertEquals("Cannot divide by zero", exception.getMessage());
    }
}
