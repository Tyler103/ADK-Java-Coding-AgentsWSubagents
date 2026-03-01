package example;

public class Main {
    public static void main(String[] args) {
        Calculator calculator = new Calculator();

        System.out.println("2 + 3 = " + calculator.add(2, 3));
        System.out.println("5 - 2 = " + calculator.sub(5, 2));
        System.out.println("4 * 6 = " + calculator.mul(4, 6));
        System.out.println("10 / 2 = " + calculator.div(10, 2));

        try {
            calculator.div(10, 0);
        } catch (IllegalArgumentException e) {
            System.out.println("Error: " + e.getMessage());
        }
    }
}
